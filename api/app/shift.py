"""submitShift plausibility rules (S3). Pure functions; `GameService.command` calls `validate_shift` under the factory
lock BEFORE the sim, so an impossible shift costs no sim call, no version bump and no shift of the daily limit
(that counter stays in the factory state, the sim keeps its own checks as a second line).

The thresholds come from the real clients (vendor/frontend/web/src/lib/minigames: ShiftRunner.ts, games/*.ts) and are
deliberately wide: no honest client may ever be rejected.
  * durationMs = round(playT * 1000) of one round, a round lasts baseDuration x (1 - 0.4 x difficulty) <= 8 s;
  * score = clamp(metrics.score ?? win 0.55..1 / fail 0.15..0.35, 0, 1); a WIN is never below 0.4;
  * metrics: accuracy 0..1, misses a count, reaction_ms measured from setup (>= 0.8 s verb phase), dodged / fail_reason_len
    are counts, `score` of the stacker is 0.4..1.
"""
from __future__ import annotations

import math
from typing import Any

MIN_DURATION_MS = 400            # spec (Блок 8, S3); also the mock's own limit
MAX_DURATION_MS = 60_000         # spec; real rounds are <= 8 s
WIN_MIN_SCORE = 0.1              # real wins are >= 0.4: a "win" scoring ~0 contradicts itself
ACCURACY_EPS = 1e-6              # float noise of inZone / total
REACTION_MS = (80.0, 10_000.0)   # no human reacts faster than 80 ms; a 10 s reaction is a stall, not a measurement
MAX_COUNT = 1000                 # misses / hits
MAX_METRICS = 32
MAX_METRIC_KEY = 40
MAX_METRIC_ABS = 1e6
MAX_GAME_ID = 64

# rule -> (top-level error code, message). `details.reason` of the 422 is the rule name.
RULES: dict[str, tuple[str, str]] = {
    "minigame_too_short": ("minigame_too_short", "Смена не засчитана: слишком быстро"),
    "duration_too_long": ("impossible_score", "Смена не засчитана: слишком долго"),
    "duration_invalid": ("impossible_score", "Смена не засчитана: некорректная длительность"),
    "score_not_finite": ("impossible_score", "Смена не засчитана: некорректный счёт"),
    "score_out_of_range": ("impossible_score", "Смена не засчитана: счёт вне диапазона 0..1"),
    "difficulty_out_of_range": ("impossible_score", "Смена не засчитана: сложность вне диапазона 0..1"),
    "metrics_invalid": ("impossible_score", "Смена не засчитана: некорректные метрики"),
    "accuracy_out_of_range": ("impossible_score", "Смена не засчитана: точность вне диапазона 0..1"),
    "misses_invalid": ("impossible_score", "Смена не засчитана: некорректное число промахов"),
    "hits_invalid": ("impossible_score", "Смена не засчитана: некорректное число попаданий"),
    "reaction_out_of_range": ("impossible_score", "Смена не засчитана: нереалистичная реакция"),
    "win_score_too_low": ("impossible_score", "Смена не засчитана: победа с нулевым счётом"),
    "game_id_invalid": ("impossible_score", "Смена не засчитана: неизвестная миниигра"),
}
RULE_CODES: dict[str, str] = {rule: code for rule, (code, _) in RULES.items()}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v: Any) -> bool:
    return _num(v) and math.isfinite(v)


def _count_ok(v: Any) -> bool:
    return _finite(v) and v == int(v) and 0 <= v <= MAX_COUNT


def check_metrics(m: Any) -> str | None:
    if not isinstance(m, dict) or len(m) > MAX_METRICS:
        return "metrics_invalid"
    for k, v in m.items():
        if not isinstance(k, str) or len(k) > MAX_METRIC_KEY or not _finite(v) or abs(v) > MAX_METRIC_ABS:
            return "metrics_invalid"
    acc = m.get("accuracy")
    if acc is not None and not -ACCURACY_EPS <= acc <= 1 + ACCURACY_EPS:
        return "accuracy_out_of_range"
    if "misses" in m and not _count_ok(m["misses"]):
        return "misses_invalid"
    if "hits" in m and not _count_ok(m["hits"]):
        return "hits_invalid"
    rt = m.get("reaction_ms")
    if rt is not None and not REACTION_MS[0] <= rt <= REACTION_MS[1]:
        return "reaction_out_of_range"
    return None


def validate_shift(r: Any) -> str | None:
    """Name of the first violated rule of a ShiftResult (wire dict), or None when the shift is plausible."""
    if not isinstance(r, dict):
        return "metrics_invalid"
    dur, score, diff = r.get("durationMs"), r.get("score"), r.get("difficulty")
    if not _finite(dur):
        return "duration_invalid"
    if dur < MIN_DURATION_MS:
        return "minigame_too_short"
    if dur > MAX_DURATION_MS:
        return "duration_too_long"
    if not _finite(score):
        return "score_not_finite"
    if not 0 <= score <= 1:
        return "score_out_of_range"
    if not _finite(diff) or not 0 <= diff <= 1:
        return "difficulty_out_of_range"
    game_id = r.get("gameId")
    if not isinstance(game_id, str) or not 1 <= len(game_id) <= MAX_GAME_ID:
        return "game_id_invalid"
    bad = check_metrics(r.get("metrics"))
    if bad:
        return bad
    if r.get("result") == "win" and score < WIN_MIN_SCORE:
        return "win_score_too_low"
    return None
