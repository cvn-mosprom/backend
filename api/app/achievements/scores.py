"""Competencies and job fit (C2): pure, deterministic scoring on top of the achievements engine. No database, no clock.

Everything is an integer 0..100 and every number can be explained to HR ("почему 82"):

COMPETENCIES (six scales, catalog.COMPETENCIES)
    proof_k = 100 * sum(w[a] * credit[a]) / sum(w[a])   over the catalog achievements whose `competency` is k
              w = tier weight (bronze 1, silver 2, gold 3);  credit = 1 when earned, else PARTIAL * min(1, current / target)
    speed / accuracy / attention / endurance
            = 0.75 * skill_k * conf  +  0.25 * proof_k        skill_k = state.skills[k] (0..100, EMA of the player's shifts)
                                                              conf    = min(1, shifts_total / 10)   (a few shifts prove little)
    reliability = proof_reliability                           (production volume, SKU range, orders in time, loans repaid)
    company     = 0.5 * quiz% + 0.5 * proof_company           quiz% = 100 * correct / 8 (progress of `quiz_8`)

FIT per profession (all ProfessionIds), the sum of four parts, max 15 + 25 + 15 + 45 = 100:
    experience    15 * min(1, played / 10)
    results       25 * (0.6 * win_rate + 0.4 * avg_score) * min(1, played / 5)
    achievements  15 * (0.4 * credit(<pid>_practice) + 0.6 * credit(<pid>_ace))
    competencies  45 * sum(weight[pid][k] * competency_k / 100)   (PROFILE: what the profession asks for)
  The parts are floats; the score is round(sum) and the integer `delta`s of the reasons are the parts apportioned to that exact
  total (largest remainder), so reasons always sum to the score. A reason with delta 0 is left out (except the "no data" one).

VACANCY FIT `vacancy_fit(competency, earned_ids, requirements)`: every requirement (a minimal competency, a required achievement)
  is one equal slice of 100; a competency slice is earned in proportion value / min (capped at 1), an achievement slice is all or
  nothing. No requirements -> score 0. Reasons list every requirement (an unmet one keeps delta 0 and says so).

The weights live in this file and can be re-tuned without a migration; stored scores follow on the next evaluation.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from . import catalog as cat

TIER_WEIGHT = {"bronze": 1, "silver": 2, "gold": 3}
PARTIAL = 0.5  # credit for an achievement that is not earned yet, per share of its progress
SKILL_KEYS = ("speed", "accuracy", "attention", "endurance")
SKILL_WEIGHT, PROOF_WEIGHT = 0.75, 0.25
CONF_SHIFTS = 10
QUIZ_TOTAL = 8  # the quiz bank (quiz_8's target)
QUIZ_WEIGHT = 0.5

LABELS = {"speed": "скорость", "accuracy": "точность", "attention": "внимательность", "endurance": "выносливость",
          "reliability": "надёжность", "company": "знание компании"}

# fit parts (max points) and their saturation points
EXP_MAX, RESULTS_MAX, ACH_MAX, COMP_MAX = 15, 25, 15, 45
EXP_SHIFTS, RESULTS_SHIFTS = 10, 5
WIN_SHARE, SCORE_SHARE = 0.6, 0.4
PRACTICE_SHARE, ACE_SHARE = 0.4, 0.6

# what each profession asks for: competency -> weight, every row sums to 1
PROFILE: Mapping[str, Mapping[str, float]] = {
    "kvasovar": {"attention": 0.35, "reliability": 0.25, "accuracy": 0.2, "endurance": 0.2},
    "operator": {"speed": 0.4, "accuracy": 0.3, "attention": 0.15, "endurance": 0.15},
    "adjuster": {"accuracy": 0.35, "attention": 0.25, "reliability": 0.2, "speed": 0.2},
    "lab": {"attention": 0.45, "accuracy": 0.35, "company": 0.1, "reliability": 0.1},
    "storekeeper": {"reliability": 0.35, "accuracy": 0.25, "attention": 0.2, "endurance": 0.2},
    "forklift": {"attention": 0.3, "speed": 0.3, "reliability": 0.2, "endurance": 0.2},
    "kipia": {"accuracy": 0.35, "attention": 0.25, "company": 0.2, "reliability": 0.2},
}
assert set(PROFILE) == set(cat.PROFESSIONS), "a profession without a fit profile"
assert all(abs(sum(w.values()) - 1) < 1e-9 and set(w) <= set(cat.COMPETENCIES) for w in PROFILE.values())

_BY_COMPETENCY: dict[str, list[cat.Achievement]] = {k: [a for a in cat.CATALOG if a.competency == k] for k in cat.COMPETENCIES}


# ---------------------------------------------------------------- helpers

def _num(v: Any, default: float = 0.0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _int(v: float) -> int:
    """Round half up (Python's round() is banker's rounding: 0.5 -> 0, which would make boundaries surprising)."""
    return int(math.floor(v + 0.5 + 1e-9))


def credit(ach_id: str, earned: set[str], progress: Mapping[str, float]) -> float:
    """1 when earned, else PARTIAL * the share of the progress bar (0 when nothing is known)."""
    if ach_id in earned:
        return 1.0
    a = cat.BY_ID.get(ach_id)
    if a is None or a.target <= 0:
        return 0.0
    return PARTIAL * _clamp(_num(progress.get(ach_id)) / a.target, 0.0, 1.0)


def proof(competency: str, earned: set[str], progress: Mapping[str, float]) -> float:
    """0..100: weighted share of the achievements that prove `competency` (see the module docstring)."""
    items = _BY_COMPETENCY.get(competency, [])
    total = sum(TIER_WEIGHT[a.tier] for a in items)
    if total <= 0:
        return 0.0
    return 100.0 * sum(TIER_WEIGHT[a.tier] * credit(a.id, earned, progress) for a in items) / total


def shifts_total(state: Mapping[str, Any], by_profession: Mapping[str, Mapping[str, float]] | None = None) -> int:
    """Shifts played: `state.shiftsTotal`, or the per-profession sum when that is larger (older states)."""
    s = _num(state.get("shiftsTotal")) if isinstance(state, Mapping) else 0.0
    by = sum(_num(v.get("played")) for v in (by_profession or {}).values() if isinstance(v, Mapping))
    return int(max(s, by))


def apportion(parts: list[float], total: int) -> list[int]:
    """Integers that sum to exactly `total`, each within 1 of its part (largest remainder, ties to the earlier part)."""
    floors = [int(math.floor(p + 1e-9)) for p in parts]
    left = total - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(parts[i] - floors[i]), i))
    out = list(floors)
    for i in order[:max(left, 0)]:
        out[i] += 1
    if left < 0:  # cannot happen when total == round(sum(parts)); keep the sum honest anyway
        for i in sorted(range(len(parts)), key=lambda i: (parts[i] - floors[i], -i)):
            if left == 0:
                break
            if out[i] > 0:
                out[i] -= 1
                left += 1
    return out


# ---------------------------------------------------------------- competencies

def quiz_correct(earned: set[str], progress: Mapping[str, float]) -> int:
    """Correct quiz answers, from the stored progress of the quiz achievements (monotonic, so it needs no extra query)."""
    if "quiz_8" in earned:
        return QUIZ_TOTAL
    return int(min(QUIZ_TOTAL, max(_num(progress.get("quiz_8")), _num(progress.get("quiz_6")))))


def competencies(skills: Mapping[str, Any] | None, shifts: int, earned: Iterable[str],
                 progress: Mapping[str, float] | None = None) -> dict[str, int]:
    """The six scales 0..100 (module docstring). `skills` = state.skills, `shifts` = shifts played, `progress` = {achievement id:
    current} of the unearned ones (any extra key is harmless)."""
    ok, prog = set(earned), dict(progress or {})
    sk = skills if isinstance(skills, Mapping) else {}
    conf = _clamp(shifts / CONF_SHIFTS, 0.0, 1.0)
    out: dict[str, int] = {}
    for k in cat.COMPETENCIES:
        p = proof(k, ok, prog)
        if k in SKILL_KEYS:
            v = SKILL_WEIGHT * _clamp(_num(sk.get(k))) * conf + PROOF_WEIGHT * p
        elif k == "company":
            quiz = 100.0 * quiz_correct(ok, prog) / QUIZ_TOTAL
            v = QUIZ_WEIGHT * quiz + (1 - QUIZ_WEIGHT) * p
        else:  # reliability
            v = p
        out[k] = _int(_clamp(v))
    return out


# ---------------------------------------------------------------- fit per profession

def _prof_stats(by_profession: Mapping[str, Mapping[str, float]] | None, pid: str) -> tuple[float, float, float]:
    s = (by_profession or {}).get(pid)
    s = s if isinstance(s, Mapping) else {}
    return max(0.0, _num(s.get("played"))), max(0.0, _num(s.get("wins"))), _clamp(_num(s.get("avg", s.get("avg_score"))), 0.0, 1.0)


def fit_for(pid: str, comp: Mapping[str, int], by_profession: Mapping[str, Mapping[str, float]] | None, earned: set[str],
            progress: Mapping[str, float] | None = None) -> dict[str, Any]:
    """{score, reasons[{text, delta}]} for one profession; the deltas sum to the score."""
    prog = progress or {}
    name = cat.PROFESSIONS[pid]
    played, wins, avg = _prof_stats(by_profession, pid)
    win_rate = _clamp(wins / played, 0.0, 1.0) if played > 0 else 0.0
    parts: list[tuple[float, str]] = []
    parts.append((EXP_MAX * min(1.0, played / EXP_SHIFTS),
                  f"Опыт: {int(played)} из {EXP_SHIFTS} смен по профессии «{name}»"))
    perf = (WIN_SHARE * win_rate + SCORE_SHARE * avg) * min(1.0, played / RESULTS_SHIFTS)
    parts.append((RESULTS_MAX * perf,
                  f"Результат смен: побед {int(wins)} из {int(played)}, средний балл {_int(avg * 100)}%"))
    practice, ace = f"{pid}_practice", f"{pid}_ace"
    ach = ACH_MAX * (PRACTICE_SHARE * credit(practice, earned, prog) + ACE_SHARE * credit(ace, earned, prog))
    got = [cat.BY_ID[i].title for i in (practice, ace) if i in earned]
    parts.append((ach, "Достижения профессии: " + ", ".join(f"«{t}»" for t in got) if got
                  else "Достижения профессии: прогресс по «практике» и «асу»"))
    contrib = sorted(((COMP_MAX * w * comp.get(k, 0) / 100.0, k, w) for k, w in PROFILE[pid].items()),
                     key=lambda c: (-c[0], c[1]))
    for value, k, w in contrib[:3]:
        parts.append((value, f"Компетенция «{LABELS[k]}»: {comp.get(k, 0)} из 100 (вес {_int(w * 100)}%)"))
    rest = sum(c[0] for c in contrib[3:])
    if len(contrib) > 3:
        parts.append((rest, "Остальные компетенции профессии"))
    total = _int(sum(p for p, _ in parts))
    deltas = apportion([p for p, _ in parts], total)
    reasons = [{"text": text, "delta": d} for (_, text), d in zip(parts, deltas) if d > 0]
    if not reasons:
        reasons = [{"text": f"Пока нет подтверждённых данных для профессии «{name}»", "delta": 0}]
    return {"score": total, "reasons": reasons}


def fits(comp: Mapping[str, int], by_profession: Mapping[str, Mapping[str, float]] | None, earned: Iterable[str],
         progress: Mapping[str, float] | None = None) -> dict[str, dict[str, Any]]:
    ok = set(earned)
    return {pid: fit_for(pid, comp, by_profession, ok, progress) for pid in cat.PROFESSIONS}


def best_fit(fit: Mapping[str, Mapping[str, Any]]) -> tuple[str, int] | None:
    """(profession, score) of the strongest fit; the first profession of the catalog wins a tie; None for an empty / all-zero fit."""
    best: tuple[str, int] | None = None
    for pid in cat.PROFESSIONS:
        s = fit.get(pid)
        score = int(_num(s.get("score"))) if isinstance(s, Mapping) else 0
        if score > 0 and (best is None or score > best[1]):
            best = (pid, score)
    return best


def score_player(state: Mapping[str, Any], by_profession: Mapping[str, Mapping[str, float]] | None, earned: Iterable[str],
                 progress: Mapping[str, float] | None = None) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """(competency, fit) of one player: what is written to player_scores. `state` = factories.state."""
    ok = set(earned)
    skills = state.get("skills") if isinstance(state, Mapping) else None
    comp = competencies(skills, shifts_total(state, by_profession), ok, progress)
    return comp, fits(comp, by_profession, ok, progress)


# ---------------------------------------------------------------- vacancy fit (C5)

def vacancy_fit(competency: Mapping[str, Any] | None, earned_ids: Iterable[str],
                requirements: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fit of a player to a vacancy's requirements `{competencies: {key: min}, achievements: [id]}` -> {score, reasons}.
    The player side is `competency` (player_scores.competency) and the earned achievement ids."""
    req = requirements if isinstance(requirements, Mapping) else {}
    comp_req = req.get("competencies") if isinstance(req.get("competencies"), Mapping) else {}
    ach_req = req.get("achievements") if isinstance(req.get("achievements"), (list, tuple)) else []
    comp = competency if isinstance(competency, Mapping) else {}
    ok = set(earned_ids)
    items: list[tuple[float, str]] = []  # (share 0..1, text)
    for k in cat.COMPETENCIES:
        if k not in comp_req:
            continue
        need, have = _num(comp_req[k]), _clamp(_num(comp.get(k)))
        share = 1.0 if need <= 0 else min(1.0, have / need)
        verdict = "требование выполнено" if share >= 1 else "ниже требуемого"
        items.append((share, f"Компетенция «{LABELS[k]}»: {_int(have)} при требуемых {_int(need)}, {verdict}"))
    seen: set[str] = set()
    for aid in ach_req:
        if not isinstance(aid, str) or aid in seen:
            continue
        seen.add(aid)
        title = cat.BY_ID[aid].title if aid in cat.BY_ID else aid
        have = aid in ok
        items.append((1.0 if have else 0.0, f"Достижение «{title}»: " + ("получено" if have else "не получено")))
    if not items:
        return {"score": 0, "reasons": [{"text": "У вакансии не заданы требования", "delta": 0}]}
    slice_ = 100.0 / len(items)
    parts = [s * slice_ for s, _ in items]
    total = _int(sum(parts))
    deltas = apportion(parts, total)
    return {"score": total, "reasons": [{"text": text, "delta": d} for (_, text), d in zip(items, deltas)]}
