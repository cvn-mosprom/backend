"""C1: the pure achievements engine and the catalog. No database: every rule at the boundary (just below / exactly at the target),
determinism, cheap mode, counters (done orders seen once, loans repaid in time), tolerance of malformed states, and drift tests
that pin the catalog's game content (SKU ids, professions, facts, quests, constants) to the vendored frontend."""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from app import quiz_bank
from app.achievements import catalog as cat
from app.achievements.engine import RULES, evaluate, longest_streak, update_counters
from datetime import date

H = 3_600_000
ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "vendor" / "frontend" / "web" / "src" / "lib" / "content"
needs_vendor = pytest.mark.skipif(not (CONTENT / "products.ts").exists(), reason="vendor/frontend submodule is not checked out")


def state(*, produced: float = 0, quality: float = 1.0, skus: int = 0, done: tuple = (), facts: int = 0, referrals: int = 0,
          orders: list | None = None, loan: dict | None = None, loan_log: list | None = None, bp: dict | None = None,
          season: int = 3) -> dict[str, Any]:
    sim: dict[str, Any] = {"produced": produced, "qualitySum": produced * quality,
                           "collection": {k: 1_000 + i for i, k in enumerate(cat.SKU_IDS[:skus])}, "orders": orders or []}
    if loan:
        sim["loan"] = loan
    if loan_log:
        sim["loanLog"] = loan_log
    return {"v": 2, "season": {"id": season}, "sim": sim, "referrals": referrals,
            "dialogs": {"done": list(done), "factsSeen": list(cat.FACT_IDS[:facts]), "lastHintAt": 0},
            "skills": {"speed": 0, "accuracy": 0, "attention": 0, "endurance": 0, "byProfession": bp or {}}}


def ev(st: dict[str, Any], *, cheap: bool = False, **extra: Any) -> dict[str, Any]:
    return evaluate({"state": st, "season_id": 3, **extra}, cheap=cheap)


def ids(res: dict[str, Any]) -> set[str]:
    return {e["id"] for e in res["earned"]}


def prog(res: dict[str, Any], aid: str, key: int = 0) -> float:
    return next(p["current"] for p in res["progress"] if p["id"] == aid and p["season_key"] == key)


def order(oid: str, *, status: str = "done", min_q: float = 0.85, tutorial: bool = False) -> dict[str, Any]:
    o = {"id": oid, "status": status, "minQuality": min_q, "qty": 10, "loaded": 10 if status == "done" else 3}
    if tutorial:
        o["tutorial"] = True
    return o


def shifts(**kw: Any) -> dict[str, Any]:
    return {"by_profession": {}, "fast_reaction": 0, "perfect": 0, "days": [], "by_season": {}, **kw}


def days(start: str, n: int, per_day: int = 1, gap_after: int | None = None) -> list[list]:
    d0 = date.fromisoformat(start).toordinal()
    out, off = [], 0
    for i in range(n):
        if gap_after is not None and i == gap_after:
            off += 1  # one missing day
        out.append([date.fromordinal(d0 + i + off).isoformat(), per_day])
    return out


# ---------------------------------------------------------------- catalog

def test_catalog_shape_and_points():
    assert cat.CATALOG_VERSION >= 1 and 30 <= len(cat.CATALOG) <= 45
    assert len({a.id for a in cat.CATALOG}) == len(cat.CATALOG)
    by = {a.id: a for a in cat.CATALOG}
    # the plan's table (docs/hr-crm-plan.md): points of the named entries
    assert [by[i].points for i in ("first_line", "first_order")] == [10, 10]
    assert [by[i].points for i in ("bottles_1k", "bottles_10k", "bottles_100k")] == [10, 25, 50]
    assert [by[i].points for i in ("sku_5", "sku_all")] == [20, 50]
    assert [by[i].points for i in ("no_defects", "otk_control")] == [30, 25]
    assert [by[i].points for i in ("reaction_fast", "week_streak", "full_days")] == [25, 30, 20]
    assert [by[i].points for i in ("on_time_10", "no_debts")] == [25, 15]
    assert [by[i].points for i in ("museum_all", "quiz_6", "quiz_8")] == [20, 20, 40]
    assert [by[i].points for i in ("season_top50", "season_top10", "season_top3")] == [30, 60, 100]
    assert by["mentor"].points == 20
    for pid in cat.PROFESSIONS:
        assert by[f"{pid}_practice"].points == 10 and by[f"{pid}_ace"].points == 40 and by[f"{pid}_ace"].profession == pid
    assert {a.tier for a in cat.CATALOG} == set(cat.TIERS)
    assert sum(1 for a in cat.CATALOG if a.hidden) >= 1 and sum(1 for a in cat.CATALOG if a.seasonal) >= 4
    assert all(a.title and a.description and a.points > 0 and a.target > 0 for a in cat.CATALOG)
    assert {a.competency for a in cat.CATALOG} - {None} <= set(cat.COMPETENCIES)
    assert [a["id"] for a in cat.public_catalog()] == [a.id for a in cat.CATALOG]
    assert not any(a.id.startswith("_") for a in cat.CATALOG)


def test_every_rule_is_used_and_every_entry_has_one():
    assert {a.rule for a in cat.CATALOG} == set(RULES)


@needs_vendor
def test_catalog_content_matches_the_vendored_game():
    products = (CONTENT / "products.ts").read_text(encoding="utf-8")
    assert list(cat.SKU_IDS) == re.findall(r"id: '(\w+)', title: '[^']*', short:", products)
    facts = (CONTENT / "facts.ts").read_text(encoding="utf-8")
    assert sorted(cat.FACT_IDS) == sorted(re.findall(r"\{ id: '(f\d+)', year", facts))
    buildings = (CONTENT / "buildings.ts").read_text(encoding="utf-8")
    block = buildings.split("export type ProfessionId =")[1].split(";")[0]
    assert sorted(cat.PROFESSIONS) == sorted(re.findall(r"'(\w+)'", block))
    dialogs = (CONTENT / "dialogs.ts").read_text(encoding="utf-8")
    quests = re.findall(r"id: '(q_\w+)', who:", dialogs)
    assert cat.QUEST_LINE in quests and cat.QUEST_FIRST_SHIPMENT in quests
    balance = (CONTENT / "balance.ts").read_text(encoding="utf-8")
    assert int(re.search(r"SHIFTS_PER_DAY = (\d+)", balance).group(1)) == cat.SHIFTS_PER_DAY
    assert int(re.search(r"LOAN_OVERDUE_H = (\d+)", balance).group(1)) * H == cat.LOAN_OVERDUE_MS
    assert quiz_bank.TOTAL == next(a for a in cat.CATALOG if a.id == "quiz_8").target == 8


# ---------------------------------------------------------------- state rules, boundaries

def test_empty_state_earns_nothing_and_has_progress():
    res = ev(state())
    assert res["earned"] == [] and res["points"] == 0
    assert prog(res, "bottles_1k") == 0
    assert all(0 <= p["current"] <= p["target"] for p in res["progress"])


@pytest.mark.parametrize("n, earned", [(999, set()), (1000, {"bottles_1k"}), (9999, {"bottles_1k"}),
                                       (10_000, {"bottles_1k", "bottles_10k"}),
                                       (100_000, {"bottles_1k", "bottles_10k", "bottles_100k"})])
def test_bottles(n, earned):
    got = ids(ev(state(produced=n))) & {"bottles_1k", "bottles_10k", "bottles_100k"}
    assert got == earned


def test_bottles_progress_value():
    assert prog(ev(state(produced=250)), "bottles_1k") == 250


@pytest.mark.parametrize("n, five, allsku", [(4, False, False), (5, True, False), (7, True, False), (8, True, True)])
def test_sku_collection(n, five, allsku):
    got = ids(ev(state(skus=n)))
    assert ("sku_5" in got) is five and ("sku_all" in got) is allsku


def test_unknown_sku_keys_do_not_count():
    st = state(skus=4)
    st["sim"]["collection"].update({"beer_pet": 1, "x": 2})
    assert "sku_5" not in ids(ev(st))


@pytest.mark.parametrize("produced, q, earned", [(999, 1.0, False), (1000, 0.9799, False), (1000, 0.98, True),
                                                 (5000, 0.99, True), (50_000, 0.97, False)])
def test_no_defects(produced, q, earned):
    assert ("no_defects" in ids(ev(state(produced=produced, quality=q)))) is earned


def test_no_defects_progress_is_the_weakest_condition_and_never_100_early():
    res = ev(state(produced=500, quality=1.0))
    assert prog(res, "no_defects") == 50
    res = ev(state(produced=1000, quality=0.9799999))
    assert prog(res, "no_defects") < 100 and "no_defects" not in ids(res)


def test_quests():
    assert {"first_line", "first_order"}.isdisjoint(ids(ev(state(done=("q_intro", "q_brew")))))
    got = ids(ev(state(done=("q_intro", "q_line"))))
    assert "first_line" in got and "first_order" not in got
    assert "first_order" in ids(ev(state(done=("q_dock",))))


@pytest.mark.parametrize("n, earned", [(8, False), (9, False), (10, True)])
def test_museum(n, earned):
    assert ("museum_all" in ids(ev(state(facts=n)))) is earned


def test_museum_ignores_unknown_fact_ids():
    st = state(facts=9)
    st["dialogs"]["factsSeen"] += ["f1", "nonsense"]
    assert "museum_all" not in ids(ev(st))


@pytest.mark.parametrize("n, earned", [(2, False), (3, True), (4, True)])
def test_mentor_from_state(n, earned):
    assert ("mentor" in ids(ev(state(referrals=n)))) is earned


def test_mentor_uses_the_larger_of_state_and_users_table():
    assert "mentor" in ids(ev(state(referrals=1), referrals=3))
    assert "mentor" not in ids(ev(state(referrals=1), referrals=2))
    assert "mentor" not in ids(ev(state(referrals=0), cheap=True, referrals=9))  # the aggregate is not a cheap input


# ---------------------------------------------------------------- orders and loans (counters)

def test_on_time_orders_boundary_and_failed_orders_do_not_count():
    done = lambda n: [order(f"o{i}") for i in range(n)]  # noqa: E731
    assert "on_time_10" not in ids(ev(state(orders=done(9) + [order("late", status="failed")])))
    assert "on_time_10" in ids(ev(state(orders=done(10))))
    assert prog(ev(state(orders=done(4))), "on_time_10") == 4


def test_tutorial_order_is_a_first_order_but_not_an_on_time_one():
    res = ev(state(orders=[order("tutorial_first", tutorial=True)]))
    assert "first_order" in ids(res) and res["counters"]["done"] == 0 and res["counters"]["any"] == 1


def test_otk_orders_boundary():
    qualities = [0.9] * 9 + [0.89, 0.85]  # the 0.89 ones do not count
    orders = [order(f"o{i}", min_q=q) for i, q in enumerate(qualities)]
    assert "otk_control" not in ids(ev(state(orders=orders)))
    assert "otk_control" in ids(ev(state(orders=orders + [order("x", min_q=0.95)])))


def test_counters_count_each_order_once_and_outlive_the_state():
    st = state(orders=[order(f"o{i}") for i in range(6)])
    first = ev(st)
    again = ev(st, counters=first["counters"])
    assert again["counters"] == first["counters"] and first["counters"]["done"] == 6
    st2 = state(orders=[order(f"p{i}") for i in range(4)])  # the first six were pruned from the state, four new ones
    third = ev(st2, counters=again["counters"])
    assert third["counters"]["done"] == 10 and "on_time_10" in ids(third)
    assert ev(state(), counters=third["counters"])["counters"]["done"] == 10  # nothing in the state: the count stays


def test_seen_ids_are_bounded():
    c: Any = None
    for batch in range(5):
        c = update_counters(c, state(orders=[order(f"b{batch}-{i}") for i in range(100)]))
    assert c["done"] == 500 and len(c["seen"]) == 200


def loan(taken: int, left: float = 100, overdue: bool = False) -> dict:
    return {"principal": 100, "fee": 10, "left": left, "takenAt": taken, **({"overdueApplied": True} if overdue else {})}


def test_loan_repaid_in_time_counts_once():
    c1 = ev(state(loan=loan(1_000)))["counters"]
    assert c1["loan"] == {"takenAt": 1_000, "overdue": False} and c1["loans_ok"] == 0
    paid = [{"t": 1_000 + 5 * H, "amount": 110, "source": "manual", "left": 0}]
    res = ev(state(loan_log=paid), counters=c1)
    assert res["counters"]["loans_ok"] == 1 and res["counters"]["loan"] is None and "no_debts" in ids(res)
    assert ev(state(loan_log=paid), counters=res["counters"])["counters"]["loans_ok"] == 1  # still once


def test_loan_repaid_late_or_after_an_overdue_does_not_count():
    c1 = ev(state(loan=loan(1_000)))["counters"]
    late = [{"t": 1_000 + 48 * H, "amount": 110, "source": "manual", "left": 0}]  # exactly at the overdue point = late
    assert ev(state(loan_log=late), counters=c1)["counters"]["loans_ok"] == 0
    on_edge = [{"t": 1_000 + 48 * H - 1, "amount": 110, "source": "manual", "left": 0}]
    assert ev(state(loan_log=on_edge), counters=c1)["counters"]["loans_ok"] == 1
    c2 = ev(state(loan=loan(1_000, overdue=True)), counters=c1)["counters"]  # the overdue was seen while the loan was active
    assert c2["loan"]["overdue"] is True
    assert ev(state(loan_log=[{"t": 1_000 + 60 * H, "amount": 1, "source": "order", "left": 0}]), counters=c2)["counters"]["loans_ok"] == 0


def test_a_loan_never_seen_active_is_not_counted():
    res = ev(state(loan_log=[{"t": 5, "amount": 1, "source": "order", "left": 0}]))
    assert res["counters"]["loans_ok"] == 0 and "no_debts" not in ids(res)


def test_a_replaced_loan_counts_the_repaid_one():
    c1 = ev(state(loan=loan(1_000)))["counters"]
    log = [{"t": 1_000 + H, "amount": 110, "source": "manual", "left": 0}]
    res = ev(state(loan=loan(1_000 + 3 * H), loan_log=log), counters=c1)
    assert res["counters"]["loans_ok"] == 1 and res["counters"]["loan"]["takenAt"] == 1_000 + 3 * H


# ---------------------------------------------------------------- professions

def bp(pid: str, played: float, wins: float, avg: float) -> dict:
    return {pid: {"played": played, "wins": wins, "avgScore": avg}}


@pytest.mark.parametrize("played, earned", [(4, False), (5, True), (6, True)])
def test_profession_practice(played, earned):
    got = ids(ev(state(bp=bp("lab", played, 0, 0.1))))
    assert ("lab_practice" in got) is earned and "operator_practice" not in got


@pytest.mark.parametrize("played, wins, avg, earned", [
    (10, 7, 0.85, True), (9, 9, 0.99, False), (10, 6, 0.99, False), (10, 7, 0.8499, False), (25, 20, 0.9, True), (0, 0, 0, False)])
def test_profession_ace_boundaries(played, wins, avg, earned):
    assert ("kipia_ace" in ids(ev(state(bp=bp("kipia", played, wins, avg))))) is earned


def test_profession_achievements_carry_the_profession():
    res = ev(state(bp=bp("forklift", 30, 30, 0.95)))
    by = {e["id"]: e for e in res["earned"]}
    assert {"forklift_practice", "forklift_ace"} <= set(by)
    assert by["forklift_ace"]["evidence"]["profession"] == "forklift" and by["forklift_ace"]["evidence"]["played"] == 30
    assert cat.BY_ID["forklift_ace"].profession == "forklift"


def test_all_rounder():
    six = {p: v for pid in list(cat.PROFESSIONS)[:6] for p, v in bp(pid, 5, 3, 0.5).items()}
    assert "all_rounder" not in ids(ev(state(bp=six)))
    seven = {**six, **bp(list(cat.PROFESSIONS)[6], 5, 3, 0.5)}
    assert "all_rounder" in ids(ev(state(bp=seven)))
    six[list(cat.PROFESSIONS)[0]]["played"] = 4
    assert "all_rounder" not in ids(ev(state(bp=six)))


def test_profession_numbers_come_from_the_table_when_it_has_more_shifts():
    table = shifts(by_profession={"lab": {"played": 12, "wins": 10, "avg_score": 0.9}})
    got = ids(ev(state(bp=bp("lab", 2, 1, 0.5)), shifts=table))
    assert {"lab_practice", "lab_ace"} <= got
    assert "lab_ace" not in ids(ev(state(bp=bp("lab", 2, 1, 0.5))))  # the table is not a cheap-hook input... (not passed)
    assert "lab_practice" not in ids(ev(state(bp=bp("lab", 2, 1, 0.5)), shifts=table, cheap=True))


# ---------------------------------------------------------------- shifts, quiz, seasons

@pytest.mark.parametrize("n, earned", [(9, False), (10, True), (11, True)])
def test_reaction(n, earned):
    assert ("reaction_fast" in ids(ev(state(), shifts=shifts(fast_reaction=n)))) is earned


@pytest.mark.parametrize("perfect, earned", [(0, False), (1, True)])
def test_perfect_shift_is_hidden_and_needs_one(perfect, earned):
    assert ("perfect_shift" in ids(ev(state(), shifts=shifts(perfect=perfect)))) is earned
    assert cat.BY_ID["perfect_shift"].hidden


@pytest.mark.parametrize("n, gap, earned", [(6, None, False), (7, None, True), (10, None, True), (12, 6, False), (13, 6, True)])
def test_week_streak(n, gap, earned):
    res = ev(state(), shifts=shifts(days=days("2026-10-01", n, gap_after=gap)))
    assert ("week_streak" in ids(res)) is earned


def test_week_streak_evidence_and_zero_shift_days():
    res = ev(state(), shifts=shifts(days=days("2026-10-01", 7)))
    e = next(x for x in res["earned"] if x["id"] == "week_streak")
    assert e["evidence"] == {"days": 7, "to": "2026-10-07", "from": "2026-10-01"}
    rows = days("2026-10-01", 7)
    rows[3][1] = 0  # a day with no shift breaks the run
    assert "week_streak" not in ids(ev(state(), shifts=shifts(days=rows)))
    assert longest_streak([]) == (0, None)
    assert longest_streak([date(2026, 1, 1), date(2026, 1, 3)])[0] == 1


@pytest.mark.parametrize("full, earned", [(4, False), (5, True)])
def test_full_days(full, earned):
    rows = days("2026-10-01", full, per_day=5) + days("2026-11-01", 6, per_day=4)  # 4 shifts a day do not count
    assert ("full_days" in ids(ev(state(), shifts=shifts(days=rows)))) is earned
    assert prog(ev(state(), shifts=shifts(days=rows)), "full_days") == min(full, 5)


@pytest.mark.parametrize("correct, q6, q8", [(5, False, False), (6, True, False), (7, True, False), (8, True, True)])
def test_quiz(correct, q6, q8):
    got = ids(ev(state(), quiz={"correct": correct, "answered": 8}))
    assert ("quiz_6" in got) is q6 and ("quiz_8" in got) is q8


@pytest.mark.parametrize("place, t50, t10, t3", [(51, False, False, False), (50, True, False, False), (11, True, False, False),
                                                 (10, True, True, False), (4, True, True, False), (3, True, True, True),
                                                 (1, True, True, True)])
def test_season_place(place, t50, t10, t3):
    res = ev(state(), season_results=[{"season_id": 2, "place": place}])
    got = {(e["id"], e["season_key"]) for e in res["earned"]}
    assert (("season_top50", 2) in got) is t50 and (("season_top10", 2) in got) is t10 and (("season_top3", 2) in got) is t3
    assert all(k == 2 for _, k in got)  # seasonal: keyed by the season id


def test_season_place_is_per_season_and_best_place_wins():
    res = ev(state(), season_results=[{"season_id": 1, "place": 2}, {"season_id": 2, "place": 40}, {"season_id": 2, "place": 60}])
    got = {(e["id"], e["season_key"]) for e in res["earned"]}
    assert got == {("season_top50", 1), ("season_top10", 1), ("season_top3", 1), ("season_top50", 2)}
    assert not any(p["id"].startswith("season_top") for p in res["progress"])  # placements have no progress bar


def test_season_regular_per_season_and_progress_for_the_current_one():
    res = ev(state(), shifts=shifts(by_season={1: 15, 2: 14, 3: 6}))
    assert {(e["id"], e["season_key"]) for e in res["earned"]} == {("season_regular", 1)}
    assert prog(res, "season_regular", 3) == 6  # progress only for the current season (3)
    assert not any(p["id"] == "season_regular" and p["season_key"] != 3 for p in res["progress"])
    assert prog(ev(state(), shifts=shifts()), "season_regular", 3) == 0  # the current season has a bar even with no shifts yet


# ---------------------------------------------------------------- engine contract

def test_rules_without_their_data_are_not_evaluated_and_cheap_mode_ignores_extras():
    base = ev(state())
    assert not {"reaction_fast", "week_streak", "full_days", "quiz_6", "quiz_8", "season_top3", "season_regular",
                "perfect_shift"} & {p["id"] for p in base["progress"]}
    extras = dict(shifts=shifts(fast_reaction=99, perfect=9, days=days("2026-10-01", 9, 5)), quiz={"correct": 8, "answered": 8},
                  season_results=[{"season_id": 1, "place": 1}], referrals=9)
    assert ids(ev(state(), cheap=True, **extras)) == set()
    assert ev(state(), cheap=True, **extras) == base
    full = ev(state(), **extras)
    assert {"reaction_fast", "perfect_shift", "week_streak", "full_days", "quiz_8", "season_top3", "mentor"} <= ids(full)


def test_deterministic_and_does_not_mutate_its_input():
    st = state(produced=12_345, quality=0.99, skus=6, done=("q_line", "q_dock"), facts=10, orders=[order("a"), order("b", min_q=0.95)],
               bp=bp("lab", 12, 9, 0.9))
    bundle = {"state": st, "season_id": 3, "counters": None, "shifts": shifts(fast_reaction=10, days=days("2026-10-01", 7)),
              "quiz": {"correct": 8, "answered": 8}, "season_results": [{"season_id": 2, "place": 5}], "referrals": 1}
    before = copy.deepcopy(bundle)
    a, b = evaluate(bundle), evaluate(copy.deepcopy(bundle))
    assert a == b and json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert bundle == before
    assert a["points"] == sum(e["points"] for e in a["earned"]) > 0
    json.dumps(a)  # JSON-able (evidence goes to a jsonb column)
    order_in_catalog = [e["id"] for e in a["earned"]]
    assert order_in_catalog == sorted(order_in_catalog, key=[x.id for x in cat.CATALOG].index)


def test_malformed_states_never_raise():
    for junk in (None, "x", 5, [], {}, {"sim": "no"}, {"sim": {"produced": "lots", "orders": {"a": 1}, "collection": []}},
                 {"dialogs": {"done": "q_line", "factsSeen": [1, None]}, "skills": {"byProfession": {"lab": 3}}},
                 {"sim": {"orders": [None, 3, {"status": "done"}, {"id": 7, "status": "done"}], "loan": "x", "loanLog": [1]}},
                 {"sim": {"produced": float("nan"), "qualitySum": float("inf")}}):
        res = evaluate({"state": junk, "season_id": "x", "shifts": {"days": [None, ["bad"], ["2026-01-01", "n"]],
                                                                   "by_season": {"x": 1}, "by_profession": []},
                        "season_results": [None, {"season_id": "a"}], "quiz": {"correct": "3"}})
        assert res["version"] == cat.CATALOG_VERSION and isinstance(res["earned"], list)
        json.dumps(res)


def test_progress_is_capped_and_earned_rows_have_the_catalog_values():
    res = ev(state(produced=10_000_000, skus=8, facts=10, referrals=50))
    assert all(0 <= p["current"] <= p["target"] for p in res["progress"])
    for e in res["earned"]:
        a = cat.BY_ID[e["id"]]
        assert (e["tier"], e["points"]) == (a.tier, a.points)


def test_cheap_mode_is_fast_on_a_realistic_state():
    st = state(produced=50_000, quality=0.97, skus=8, done=("q_intro", "q_line", "q_dock"), facts=7,
               orders=[order(f"o{i}", status="done" if i % 2 else "accepted") for i in range(40)],
               bp={p: {"played": 8, "wins": 6, "avgScore": 0.8} for p in cat.PROFESSIONS})
    st["sim"]["layout"] = {"buildings": [{"id": f"b{i}", "kind": "filler", "x": i, "y": 1, "pad": "x" * 200} for i in range(150)]}
    st["history"] = {"undo": [{"p": {"k": "v" * 400}}] * 20, "redo": []}
    counters = None
    t0 = time.perf_counter()
    for _ in range(200):
        counters = evaluate({"state": st, "season_id": 3, "counters": counters}, cheap=True)["counters"]
    per_call_ms = (time.perf_counter() - t0) / 200 * 1000
    assert per_call_ms < 5, f"cheap evaluation took {per_call_ms:.2f} ms"
