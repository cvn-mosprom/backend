"""C2: the pure scoring (app/achievements/scores.py): competencies, fit per profession, vacancy fit. No database.
Boundaries on both sides, reasons that sum to the score, determinism."""
from __future__ import annotations

import math
import random

import pytest

from app.achievements import catalog as cat
from app.achievements import scores as sc

PIDS = list(cat.PROFESSIONS)
ALL_IDS = {a.id for a in cat.CATALOG}
FULL_SKILLS = {"speed": 100, "accuracy": 100, "attention": 100, "endurance": 100}


def comp(**kw: int) -> dict[str, int]:
    return {**{k: 0 for k in cat.COMPETENCIES}, **kw}


def stats(played: int = 0, wins: int = 0, avg: float = 0.0) -> dict[str, float]:
    return {"played": played, "wins": wins, "avg": avg}


# ---------------------------------------------------------------- the model itself

def test_every_competency_has_proof_and_every_profession_a_profile():
    assert all(sc._BY_COMPETENCY[k] for k in cat.COMPETENCIES)
    assert set(sc.PROFILE) == set(cat.PROFESSIONS)
    for pid, weights in sc.PROFILE.items():
        assert math.isclose(sum(weights.values()), 1.0) and set(weights) <= set(cat.COMPETENCIES), pid
    assert sc.EXP_MAX + sc.RESULTS_MAX + sc.ACH_MAX + sc.COMP_MAX == 100
    assert math.isclose(sc.SKILL_WEIGHT + sc.PROOF_WEIGHT, 1.0)


# ---------------------------------------------------------------- competencies

def test_nothing_known_is_all_zero():
    assert sc.competencies({}, 0, []) == comp()
    assert sc.competencies(None, 0, [], None) == comp()


def test_perfect_player_is_100_everywhere():
    got = sc.competencies(FULL_SKILLS, 50, ALL_IDS, {})
    assert got == {k: 100 for k in cat.COMPETENCIES}


def test_skills_without_proof_cap_at_75_and_need_shifts_to_count():
    full = sc.competencies(FULL_SKILLS, 10, [])
    assert [full[k] for k in sc.SKILL_KEYS] == [75] * 4 and full["reliability"] == 0 and full["company"] == 0
    assert sc.competencies(FULL_SKILLS, 11, []) == full  # confidence saturates at 10 shifts
    # confidence boundary: 9 shifts is 90%, 5 shifts half, 0 shifts nothing
    assert sc.competencies(FULL_SKILLS, 9, [])["speed"] == round(75 * 0.9) == 68
    assert sc.competencies(FULL_SKILLS, 5, [])["speed"] == 38  # 37.5 rounds half up
    assert sc.competencies(FULL_SKILLS, 0, [])["speed"] == 0


def test_skill_values_are_clamped_and_garbage_counts_as_zero():
    got = sc.competencies({"speed": 400, "accuracy": -50, "attention": "x", "endurance": float("nan")}, 10, [])
    assert got["speed"] == 75 and got["accuracy"] == 0 and got["attention"] == 0 and got["endurance"] == 0
    assert sc.competencies({"speed": True}, 10, [])["speed"] == 0  # a bool is not a number


def test_achievements_boost_the_matching_competency_only():
    base = sc.competencies(FULL_SKILLS, 10, [])
    boosted = sc.competencies(FULL_SKILLS, 10, ["reaction_fast"])  # silver, speed's only achievement -> proof 100
    assert boosted["speed"] == 100 and boosted["accuracy"] == base["accuracy"]
    assert sc.competencies(FULL_SKILLS, 10, ["perfect_shift"])["accuracy"] == 100  # the hidden one, accuracy's only proof


def test_proof_is_weighted_by_tier_and_partial_credit_follows_progress():
    # reliability: bottles_1k b, bottles_10k s, bottles_100k g, sku_5 s, sku_all g, on_time_10 s, no_debts b = 1+2+3+2+3+2+1 = 14
    assert sc.competencies({}, 0, ["bottles_1k"])["reliability"] == round(100 * 1 / 14) == 7
    assert sc.competencies({}, 0, ["bottles_100k"])["reliability"] == round(100 * 3 / 14) == 21
    assert sc.competencies({}, 0, ["bottles_1k", "no_debts"])["reliability"] == round(100 * 2 / 14) == 14
    # a not-yet-earned achievement gives PARTIAL * progress share: 500 of 1000 bottles = 0.5 * 0.5 of a bronze
    assert sc.competencies({}, 0, [], {"bottles_1k": 500})["reliability"] == round(100 * 0.25 / 14) == 2
    # one bottle short of the target is worth less than the achievement, never more
    just_short = sc.competencies({}, 0, [], {"bottles_1k": 999})["reliability"]
    assert just_short < sc.competencies({}, 0, ["bottles_1k"])["reliability"]
    assert sc.competencies({}, 0, [], {"bottles_1k": 10**9})["reliability"] == round(100 * 0.5 / 14)  # progress share caps at 1


def test_company_mixes_the_quiz_and_the_museum():
    assert sc.competencies({}, 0, ["quiz_8", "quiz_6"])["company"] == round(0.5 * 100 + 0.5 * 100 * 5 / 7) == 86
    assert sc.competencies({}, 0, ["museum_all"])["company"] == round(0.5 * 100 * 2 / 7) == 14
    # correct answers come from the stored progress: 7 of 8 -> 87.5% quiz, plus the partial credit of quiz_8 (0.5 * 7/8 * 3 weight)
    got = sc.competencies({}, 0, ["quiz_6"], {"quiz_8": 7})["company"]
    assert got == round(0.5 * 87.5 + 0.5 * 100 * (2 + 0.5 * 0.875 * 3) / 7)
    assert sc.quiz_correct({"quiz_8"}, {}) == 8 and sc.quiz_correct(set(), {"quiz_6": 6.0}) == 6
    assert sc.quiz_correct(set(), {"quiz_8": 99}) == 8


def test_competencies_are_deterministic_and_order_free():
    ids = sorted(ALL_IDS)[:15]
    a = sc.competencies({"speed": 61, "accuracy": 72, "attention": 55, "endurance": 40}, 7, ids, {"bottles_10k": 4000})
    b = sc.competencies({"endurance": 40, "attention": 55, "accuracy": 72, "speed": 61}, 7, reversed(ids), {"bottles_10k": 4000})
    assert a == b and all(isinstance(v, int) and 0 <= v <= 100 for v in a.values())
    assert list(a) == list(cat.COMPETENCIES)


# ---------------------------------------------------------------- fit

def perfect_stats() -> dict[str, dict[str, float]]:
    return {pid: stats(10, 10, 1.0) for pid in PIDS}


def test_fit_covers_every_profession_and_a_blank_player_scores_zero_with_one_reason():
    got = sc.fits(comp(), {}, [])
    assert list(got) == PIDS
    for pid, f in got.items():
        assert f["score"] == 0 and len(f["reasons"]) == 1 and f["reasons"][0]["delta"] == 0 and cat.PROFESSIONS[pid] in f["reasons"][0]["text"]
    assert sc.best_fit(got) is None


def test_the_best_player_gets_exactly_100():
    full = comp(**{k: 100 for k in cat.COMPETENCIES})
    got = sc.fits(full, perfect_stats(), ALL_IDS)
    assert {f["score"] for f in got.values()} == {100}
    assert all(sum(r["delta"] for r in f["reasons"]) == 100 for f in got.values())


@pytest.mark.parametrize("played,expected", [(0, 0), (1, 2), (5, 8), (9, 14), (10, 15), (11, 15), (50, 15)])
def test_experience_part_saturates_at_ten_shifts(played, expected):
    f = sc.fit_for("operator", comp(), {"operator": stats(played)}, set())  # no wins, no score: only the experience part
    assert f["score"] == expected  # 1.5 * played, rounded half up, capped at 15


def test_results_part_needs_five_shifts_to_count_in_full():
    one_win = sc.fit_for("lab", comp(), {"lab": stats(1, 1, 1.0)}, set())["score"]
    five_wins = sc.fit_for("lab", comp(), {"lab": stats(5, 5, 1.0)}, set())["score"]
    # 1 shift: exp 1.5 + results 25 * 1.0 * 0.2 = 5 -> 6.5 -> 7;  5 shifts: exp 7.5 + results 25 -> 32.5 -> 33
    assert one_win == 7 and five_wins == 33
    four_wins = sc.fit_for("lab", comp(), {"lab": stats(4, 4, 1.0)}, set())["score"]
    assert four_wins == round(6 + 25 * 0.8 + 1e-9) == 26  # 4 of 5 shifts: 80% confidence
    # losing every shift but scoring 0.5 still earns the score share (40%) of the results part
    assert sc.fit_for("lab", comp(), {"lab": stats(5, 0, 0.5)}, set())["score"] == round(7.5 + 25 * 0.4 * 0.5 + 1e-9) == 13


def test_profession_achievements_and_progress_count():
    base = sc.fit_for("kipia", comp(), {}, set())["score"]
    prac = sc.fit_for("kipia", comp(), {}, {"kipia_practice"})
    ace = sc.fit_for("kipia", comp(), {}, {"kipia_practice", "kipia_ace"})
    assert base == 0 and prac["score"] == 6 and ace["score"] == 15  # 15 * 0.4 = 6, 15 * (0.4 + 0.6)
    assert "Практика: Инженер КИПиА" in prac["reasons"][0]["text"]
    partial = sc.fit_for("kipia", comp(), {}, set(), {"kipia_ace": 50})["score"]  # 0.5 * 0.5 * 0.6 * 15 = 2.25
    assert partial == 2
    other = sc.fit_for("lab", comp(), {}, {"kipia_practice", "kipia_ace"})["score"]
    assert other == 0  # another profession's achievements do not count here


def test_competency_part_follows_the_profession_profile():
    # lab: attention .45 accuracy .35 company .1 reliability .1 of 45 points
    assert sc.fit_for("lab", comp(attention=100), {}, set())["score"] == round(45 * 0.45) == 20
    assert sc.fit_for("lab", comp(speed=100, endurance=100), {}, set())["score"] == 0  # competencies lab does not ask for
    assert sc.fit_for("operator", comp(speed=100), {}, set())["score"] == 18  # 45 * 0.4
    f = sc.fit_for("operator", comp(speed=60, accuracy=40, attention=20, endurance=10), {}, set())
    texts = [r["text"] for r in f["reasons"]]
    assert any("«скорость»: 60" in t for t in texts) and any("«точность»: 40" in t for t in texts)
    assert sum(r["delta"] for r in f["reasons"]) == f["score"]


def test_reasons_always_sum_to_the_score_and_are_clean():
    rng = random.Random(2026)
    for _ in range(400):
        c = {k: rng.randint(0, 100) for k in cat.COMPETENCIES}
        by = {pid: stats(rng.randint(0, 30), 0, rng.random()) for pid in PIDS}
        for pid in by:
            by[pid]["wins"] = rng.randint(0, int(by[pid]["played"]))
        earned = {a for a in ALL_IDS if rng.random() < 0.4}
        prog = {a: rng.random() * 10 for a in ALL_IDS}
        for pid, f in sc.fits(c, by, earned, prog).items():
            assert 0 <= f["score"] <= 100 and isinstance(f["score"], int)
            deltas = [r["delta"] for r in f["reasons"]]
            assert sum(deltas) == f["score"], (pid, f)
            assert all(isinstance(d, int) and d >= 0 for d in deltas)
            assert len({r["text"] for r in f["reasons"]}) == len(f["reasons"])
            assert f["score"] == 0 or all(d > 0 for d in deltas)


def test_fit_is_deterministic_and_best_fit_prefers_the_catalog_order_on_a_tie():
    c = comp(attention=80, accuracy=70, speed=60, endurance=50, reliability=40, company=30)
    by = {"lab": stats(12, 9, 0.9), "operator": stats(3, 1, 0.5)}
    a, b = sc.fits(c, by, {"lab_practice"}), sc.fits(dict(reversed(list(c.items()))), dict(by), ["lab_practice"])
    assert a == b
    best = sc.best_fit(a)
    assert best is not None and best[0] == "lab"
    tie = {"kvasovar": {"score": 50}, "operator": {"score": 50}}
    assert sc.best_fit(tie) == ("kvasovar", 50)  # kvasovar comes first in the catalog


def test_score_player_reads_the_state():
    state = {"skills": {**FULL_SKILLS, "byProfession": {}}, "shiftsTotal": 12}
    by = {"operator": stats(12, 12, 1.0)}
    c, f = sc.score_player(state, by, {"reaction_fast"}, {})
    assert c["speed"] == 100 and f["operator"]["score"] > f["lab"]["score"]
    assert sc.shifts_total({"shiftsTotal": 3}, {"lab": stats(7)}) == 7  # the larger of the two sources
    assert sc.shifts_total({}, None) == 0
    c0, f0 = sc.score_player({}, None, [], None)
    assert c0 == comp() and all(v["score"] == 0 for v in f0.values())


# ---------------------------------------------------------------- apportion

def test_apportion_sums_exactly_and_stays_within_one():
    rng = random.Random(7)
    for _ in range(500):
        parts = [rng.random() * rng.choice([1, 5, 20]) for _ in range(rng.randint(1, 8))]
        total = sc._int(sum(parts))
        out = sc.apportion(parts, total)
        assert sum(out) == total and all(o >= 0 and abs(o - p) < 1 for o, p in zip(out, parts))
    assert sc.apportion([0.5, 0.5], 1) == [1, 0]  # ties go to the earlier part
    assert sc.apportion([], 0) == []


# ---------------------------------------------------------------- vacancy fit

REQ = {"competencies": {"speed": 60, "attention": 80}, "achievements": ["no_defects", "quiz_6"]}


def test_vacancy_without_requirements_scores_zero():
    for req in (None, {}, {"competencies": {}, "achievements": []}, "junk"):
        r = sc.vacancy_fit(comp(speed=100), ALL_IDS, req)
        assert r["score"] == 0 and len(r["reasons"]) == 1 and r["reasons"][0]["delta"] == 0


def test_vacancy_all_met_is_100_and_none_met_is_0():
    met = sc.vacancy_fit(comp(speed=60, attention=80), {"no_defects", "quiz_6"}, REQ)
    assert met["score"] == 100 and sum(r["delta"] for r in met["reasons"]) == 100 and len(met["reasons"]) == 4
    none = sc.vacancy_fit(comp(), set(), REQ)
    assert none["score"] == 0 and all(r["delta"] == 0 for r in none["reasons"]) and len(none["reasons"]) == 4
    assert any("не получено" in r["text"] for r in none["reasons"]) and any("ниже требуемого" in r["text"] for r in none["reasons"])


def test_vacancy_competency_boundary_and_partial_credit():
    at = sc.vacancy_fit(comp(speed=60), [], {"competencies": {"speed": 60}})
    below = sc.vacancy_fit(comp(speed=59), [], {"competencies": {"speed": 60}})
    above = sc.vacancy_fit(comp(speed=61), [], {"competencies": {"speed": 60}})
    assert at["score"] == above["score"] == 100 and below["score"] == round(100 * 59 / 60) == 98
    half = sc.vacancy_fit(comp(speed=30), [], {"competencies": {"speed": 60}})
    assert half["score"] == 50 and half["reasons"][0]["delta"] == 50
    assert sc.vacancy_fit(comp(), [], {"competencies": {"speed": 0}})["score"] == 100  # a zero minimum is always met


def test_vacancy_slices_are_equal_and_reasons_sum_to_the_score():
    # 4 requirements = 25 points each: speed met (25), attention 40/80 (12.5), no_defects earned (25), quiz_6 missing (0)
    r = sc.vacancy_fit(comp(speed=70, attention=40), {"no_defects"}, REQ)
    assert r["score"] == 63 and sum(x["delta"] for x in r["reasons"]) == 63  # 62.5 rounds half up
    rng = random.Random(3)
    for _ in range(300):
        c = {k: rng.randint(0, 100) for k in cat.COMPETENCIES}
        req = {"competencies": {k: rng.randint(0, 100) for k in cat.COMPETENCIES if rng.random() < 0.5},
               "achievements": [a for a in sorted(ALL_IDS) if rng.random() < 0.1]}
        got = sc.vacancy_fit(c, {a for a in ALL_IDS if rng.random() < 0.5}, req)
        assert 0 <= got["score"] <= 100 and sum(x["delta"] for x in got["reasons"]) == got["score"]


def test_vacancy_tolerates_unknown_ids_duplicates_and_junk():
    r = sc.vacancy_fit(comp(), {"no_defects"}, {"competencies": {"nope": 50}, "achievements": ["no_defects", "no_defects", "ghost", 5]})
    assert len(r["reasons"]) == 2  # the unknown competency is ignored, the duplicate and the non-string dropped
    assert r["score"] == 50 and any("ghost" in x["text"] for x in r["reasons"])
    assert sc.vacancy_fit(None, None or [], {"competencies": {"speed": 10}})["score"] == 0
