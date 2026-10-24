"""Achievements engine (C1): a pure, deterministic function from a data bundle to earned achievements and progress.

No database, no clock, no randomness: the same bundle always gives the same answer (tests pin this). The service
(`service.py`) gathers the bundle, calls `evaluate`, and writes the difference; it never revokes anything the engine
stopped reporting (an earned achievement stays earned).

Bundle (`inputs`), every key optional except `state`:
  state           factories.state (frontend core FactoryState v2; read defensively, any missing field counts as 0)
  season_id       the server season the player is in now (seasonal progress is reported for it)
  counters        the engine's previous monotonic counters (service: achievement_progress `_counters`.meta), see update_counters
  shifts          aggregates of the `shifts` table:
                  {by_profession: {pid: {played, wins, avg_score}}, fast_reaction, perfect,
                   days: [[iso MSK day, n shifts]...], by_season: {season_id: n shifts}}
  season_results  [{season_id, place}] of closed seasons (bots excluded by the caller)
  quiz            {correct, answered} over the current bank
  referrals       number of users who joined with `ref_<player id>` (merged with state.referrals)

`cheap=True` evaluates only the rules that need the state alone (the hook after every game command): extra aggregates are
ignored, so the cost is a few dict reads.

Result: {version, earned: [{id, season_key, tier, points, evidence}], progress: [{id, season_key, current, target}],
counters, points}. `earned` lists everything currently satisfied (not only new); `progress` has `current <= target`.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Callable

from . import catalog as cat
from .catalog import Achievement

EPS = 1e-9
SEEN_KEEP = 200  # done-order ids remembered by the counters (the sim drops closed orders a day after acceptance)

Entry = tuple[int, float, "dict[str, Any] | None"]  # (season_key, current, evidence)


# ---------------------------------------------------------------- tolerant readers

def _num(v: Any, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _int(v: Any, default: int | None = None) -> int | None:
    n = _num(v, math.nan)
    return int(n) if not math.isnan(n) else default


# ---------------------------------------------------------------- counters (things the state forgets)

def empty_counters() -> dict[str, Any]:
    return {"v": 1, "any": 0, "done": 0, "otk": 0, "seen": [], "loan": None, "loans_ok": 0}


def update_counters(prev: Any, state: Any) -> dict[str, Any]:
    """Monotonic counters for facts the game state forgets: the sim drops a closed order a day after it was accepted and a repaid
    loan at once. Seen done orders are remembered by id (so an order is counted once however often the state is evaluated).
    `done` = done non-tutorial orders (the sim closes an order after its deadline as `failed`, so `done` is always in time);
    `any` also counts the tutorial order; `otk` = done orders with minQuality >= 0.9; `loans_ok` = loans repaid before the
    48 h overdue point (seen active at least once, then gone with a zero-balance payment in `loanLog`)."""
    c = empty_counters()
    p = _d(prev)
    for k in ("any", "done", "otk", "loans_ok"):
        c[k] = max(0, _int(p.get(k), 0) or 0)
    c["seen"] = [s for s in _l(p.get("seen")) if isinstance(s, str)][-SEEN_KEEP:]
    pl = _d(p.get("loan"))
    c["loan"] = ({"takenAt": _int(pl.get("takenAt"), 0), "overdue": bool(pl.get("overdue"))}
                 if pl and _int(pl.get("takenAt")) is not None else None)
    sim = _d(_d(state).get("sim"))
    seen = set(c["seen"])
    for o in _l(sim.get("orders")):
        if not isinstance(o, dict) or o.get("status") != "done":
            continue
        oid = o.get("id")
        if not isinstance(oid, str) or oid in seen:
            continue
        seen.add(oid)
        c["seen"].append(oid)
        c["any"] += 1
        if o.get("tutorial") is True:
            continue
        c["done"] += 1
        if _num(o.get("minQuality")) >= cat.OTK_MIN_QUALITY - EPS:
            c["otk"] += 1
    c["seen"] = c["seen"][-SEEN_KEEP:]
    loan = _d(sim.get("loan"))
    active = _num(loan.get("left")) > 0 and _int(loan.get("takenAt")) is not None
    taken = _int(loan.get("takenAt"), 0) if active else None
    seen_loan = c["loan"]
    if seen_loan is not None and (not active or taken != seen_loan["takenAt"]):
        # the loan we saw is gone: repaid. In time when no overdue was ever seen and the zero-balance payment came before 48 h.
        t0 = seen_loan["takenAt"] or 0
        paid_at = next((_num(e.get("t")) for e in reversed(_l(sim.get("loanLog")))
                        if isinstance(e, dict) and _num(e.get("left"), 1.0) <= 0 and _num(e.get("t")) >= t0), None)
        if not seen_loan["overdue"] and (paid_at is None or paid_at - t0 < cat.LOAN_OVERDUE_MS):
            c["loans_ok"] += 1
        c["loan"] = seen_loan = None
    if active:
        overdue = bool(loan.get("overdueApplied"))
        c["loan"] = {"takenAt": taken, "overdue": overdue or bool(seen_loan and seen_loan["overdue"])}
    return c


# ---------------------------------------------------------------- the bundle as the rules see it

class _Ctx:
    def __init__(self, inputs: dict[str, Any], cheap: bool, counters: dict[str, Any]) -> None:
        st = _d(inputs.get("state"))
        sim = _d(st.get("sim"))
        self.cheap = cheap
        self.counters = counters
        self.season_id = _int(inputs.get("season_id"))
        if self.season_id is None:
            self.season_id = _int(_d(st.get("season")).get("id"))
        self.produced = _num(sim.get("produced"))
        self.quality = _num(sim.get("qualitySum")) / self.produced if self.produced > 0 else 0.0
        collection = _d(sim.get("collection"))
        self.skus = [k for k in cat.SKU_IDS if k in collection]
        dialogs = _d(st.get("dialogs"))
        self.done = {s for s in _l(dialogs.get("done")) if isinstance(s, str)}
        seen = {s for s in _l(dialogs.get("factsSeen")) if isinstance(s, str)}
        self.facts = [f for f in cat.FACT_IDS if f in seen]
        self.state_referrals = _num(st.get("referrals"))
        self.shifts: dict[str, Any] | None = None if cheap or not isinstance(inputs.get("shifts"), dict) else inputs["shifts"]
        sr = inputs.get("season_results")
        self.season_results: list[Any] | None = None if cheap or not isinstance(sr, list) else sr
        quiz = inputs.get("quiz")
        self.quiz: dict[str, Any] | None = None if cheap or not isinstance(quiz, dict) else quiz
        self.referrals_agg = 0.0 if cheap else _num(inputs.get("referrals"))
        self.by_prof = self._professions(_d(_d(st.get("skills")).get("byProfession")),
                                         _d(self.shifts.get("by_profession")) if self.shifts else {})

    @staticmethod
    def _professions(state_bp: dict[str, Any], table_bp: dict[str, Any]) -> dict[str, dict[str, float]]:
        """Per profession: {played, wins, avg} from the state (skills.byProfession) or from the `shifts` table, whichever has
        played more (they agree in production; the table also covers factories created before the state tracked it)."""
        out: dict[str, dict[str, float]] = {}
        for pid in cat.PROFESSIONS:
            s, t = _d(state_bp.get(pid)), _d(table_bp.get(pid))
            ps, pt = _num(s.get("played")), _num(t.get("played"))
            if pt > ps:
                out[pid] = {"played": pt, "wins": _num(t.get("wins")), "avg": _num(t.get("avg_score"))}
            else:
                out[pid] = {"played": ps, "wins": _num(s.get("wins")), "avg": _num(s.get("avgScore"))}
        return out

    def available(self, source: str) -> bool:
        if source == "state":
            return True
        return {"shifts": self.shifts, "season": self.season_results, "quiz": self.quiz}[source] is not None


# ---------------------------------------------------------------- rules: (ctx, achievement) -> [(season_key, current, evidence)]

def _one(current: float, evidence: dict[str, Any]) -> list[Entry]:
    return [(0, float(current), evidence)]


def r_quest(c: _Ctx, a: Achievement) -> list[Entry]:
    q = a.params["quest"]
    return _one(1.0 if q in c.done else 0.0, {"quest": q})


def r_first_order(c: _Ctx, a: Achievement) -> list[Entry]:
    shipped = c.counters["any"] > 0 or cat.QUEST_FIRST_SHIPMENT in c.done
    return _one(1.0 if shipped else 0.0, {"orders": c.counters["any"], "quest": cat.QUEST_FIRST_SHIPMENT in c.done})


def r_produced(c: _Ctx, a: Achievement) -> list[Entry]:
    return _one(c.produced, {"produced": int(c.produced)})


def r_skus(c: _Ctx, a: Achievement) -> list[Entry]:
    return _one(len(c.skus), {"skus": list(c.skus)})


def r_no_defects(c: _Ctx, a: Achievement) -> list[Entry]:
    need, q = a.params["min_bottles"], a.params["quality"]
    cur = min(c.produced / need, c.quality / q) * 100 if c.produced > 0 else 0.0
    return _one(cur, {"produced": int(c.produced), "quality": round(c.quality, 4)})


def r_orders_otk(c: _Ctx, a: Achievement) -> list[Entry]:
    return _one(c.counters["otk"], {"orders": c.counters["otk"], "minQuality": cat.OTK_MIN_QUALITY})


def r_orders_done(c: _Ctx, a: Achievement) -> list[Entry]:
    return _one(c.counters["done"], {"orders": c.counters["done"]})


def r_loans_repaid(c: _Ctx, a: Achievement) -> list[Entry]:
    return _one(c.counters["loans_ok"], {"loans": c.counters["loans_ok"]})


def _prof_evidence(pid: str, bp: dict[str, float]) -> dict[str, Any]:
    return {"profession": pid, "played": int(bp["played"]), "wins": int(bp["wins"]), "avgScore": round(bp["avg"], 3)}


def r_prof_played(c: _Ctx, a: Achievement) -> list[Entry]:
    pid = a.params["profession"]
    bp = c.by_prof[pid]
    return _one(bp["played"], _prof_evidence(pid, bp))


def r_prof_ace(c: _Ctx, a: Achievement) -> list[Entry]:
    pid, p = a.params["profession"], a.params
    bp = c.by_prof[pid]
    if bp["played"] <= 0:
        return _one(0.0, _prof_evidence(pid, bp))
    cur = min(bp["played"] / p["min_played"], (bp["wins"] / bp["played"]) / p["win_rate"], bp["avg"] / p["avg_score"]) * 100
    ev = _prof_evidence(pid, bp)
    ev["winRate"] = round(bp["wins"] / bp["played"], 3)
    return _one(cur, ev)


def r_all_rounder(c: _Ctx, a: Achievement) -> list[Entry]:
    ok = [p for p, bp in c.by_prof.items() if bp["played"] >= a.params["min_played"]]
    return _one(len(ok), {"professions": ok})


def r_museum(c: _Ctx, a: Achievement) -> list[Entry]:
    return _one(len(c.facts), {"facts": len(c.facts)})


def r_referrals(c: _Ctx, a: Achievement) -> list[Entry]:
    n = max(c.state_referrals, c.referrals_agg)
    return _one(n, {"invited": int(n)})


def r_quiz_correct(c: _Ctx, a: Achievement) -> list[Entry]:
    q = c.quiz or {}
    return _one(_num(q.get("correct")), {"correct": int(_num(q.get("correct"))), "answered": int(_num(q.get("answered")))})


def r_reaction_fast(c: _Ctx, a: Achievement) -> list[Entry]:
    n = _num((c.shifts or {}).get("fast_reaction"))
    return _one(n, {"shifts": int(n), "belowMs": cat.FAST_REACTION_MS})


def r_perfect_shift(c: _Ctx, a: Achievement) -> list[Entry]:
    n = _num((c.shifts or {}).get("perfect"))
    return _one(n, {"shifts": int(n), "minScore": cat.PERFECT_SCORE})


def _days(c: _Ctx) -> list[tuple[date, int]]:
    out: dict[date, int] = {}
    for row in _l((c.shifts or {}).get("days")):
        try:
            day, n = row[0], int(row[1])
            d = day if isinstance(day, date) else date.fromisoformat(str(day))
        except (TypeError, ValueError, IndexError):
            continue
        out[d] = out.get(d, 0) + max(0, n)
    return sorted(out.items())


def longest_streak(days: list[date]) -> tuple[int, date | None]:
    """Longest run of consecutive calendar days in a sorted list of distinct days -> (length, last day of the run)."""
    best, best_end, run = 0, None, 0
    prev: date | None = None
    for d in days:
        run = run + 1 if prev is not None and d.toordinal() == prev.toordinal() + 1 else 1
        if run > best:
            best, best_end = run, d
        prev = d
    return best, best_end


def r_week_streak(c: _Ctx, a: Achievement) -> list[Entry]:
    n, end = longest_streak([d for d, k in _days(c) if k > 0])
    ev: dict[str, Any] = {"days": n}
    if end is not None:
        ev["to"] = end.isoformat()
        ev["from"] = date.fromordinal(end.toordinal() - n + 1).isoformat()
    return _one(n, ev)


def r_full_days(c: _Ctx, a: Achievement) -> list[Entry]:
    full = [d.isoformat() for d, k in _days(c) if k >= cat.SHIFTS_PER_DAY]
    return _one(len(full), {"days": len(full), "last": full[-3:]})


def r_season_place(c: _Ctx, a: Achievement) -> list[Entry]:
    best: dict[int, int] = {}
    for r in c.season_results or []:
        sid, place = _int(_d(r).get("season_id")), _int(_d(r).get("place"))
        if sid is not None and place is not None and place > 0:
            best[sid] = min(place, best.get(sid, place))
    mx = a.params["max_place"]
    return [(sid, 1.0 if place <= mx else 0.0, {"seasonId": sid, "place": place}) for sid, place in best.items()]


def r_season_shifts(c: _Ctx, a: Achievement) -> list[Entry]:
    out: list[Entry] = []
    for k, n in _d((c.shifts or {}).get("by_season")).items():
        sid = _int(k)
        if sid is not None:
            out.append((sid, _num(n), {"seasonId": sid, "shifts": int(_num(n))}))
    return out


RULES: dict[str, Callable[[_Ctx, Achievement], list[Entry]]] = {
    "quest": r_quest, "first_order": r_first_order, "produced": r_produced, "skus": r_skus, "no_defects": r_no_defects,
    "orders_otk": r_orders_otk, "orders_done": r_orders_done, "loans_repaid": r_loans_repaid,
    "prof_played": r_prof_played, "prof_ace": r_prof_ace, "all_rounder": r_all_rounder, "museum": r_museum,
    "referrals": r_referrals, "quiz_correct": r_quiz_correct, "reaction_fast": r_reaction_fast,
    "perfect_shift": r_perfect_shift, "week_streak": r_week_streak, "full_days": r_full_days,
    "season_place": r_season_place, "season_shifts": r_season_shifts,
}
assert {a.rule for a in cat.CATALOG} <= set(RULES), "catalog entry without a rule"


# ---------------------------------------------------------------- evaluate

def evaluate(inputs: dict[str, Any], *, cheap: bool = False) -> dict[str, Any]:
    """See the module docstring. Deterministic; never raises on malformed state (a field it cannot read counts as 0)."""
    counters = update_counters(inputs.get("counters"), inputs.get("state"))
    c = _Ctx(inputs, cheap, counters)
    earned: list[dict[str, Any]] = []
    progress: list[dict[str, Any]] = []
    for a in cat.CATALOG:
        if not c.available(a.source):
            continue
        entries = list(RULES[a.rule](c, a))
        if a.seasonal and a.progress and c.season_id is not None and all(k != c.season_id for k, _, _ in entries):
            entries.append((c.season_id, 0.0, None))
        for key, cur, evidence in sorted(entries, key=lambda e: e[0]):
            season_key = int(key) if a.seasonal else 0
            hit = cur + EPS >= a.target
            if hit:
                earned.append({"id": a.id, "season_key": season_key, "tier": a.tier, "points": a.points,
                               "evidence": evidence or {}})
            if a.progress and (not a.seasonal or season_key == c.season_id):
                shown = float(a.target) if hit else math.floor(max(cur, 0.0) * 1e4) / 1e4  # never "100/100" before it is earned
                progress.append({"id": a.id, "season_key": season_key, "current": shown, "target": float(a.target)})
    return {"version": cat.CATALOG_VERSION, "earned": earned, "progress": progress, "counters": counters,
            "points": sum(e["points"] for e in earned)}
