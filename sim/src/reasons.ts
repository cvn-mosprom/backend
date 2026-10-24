// Rejection reason codes (docs/ARCHITECTURE.md, "S2 decisions"). `/v1/command` returns
// `{ok:false, error:<ru message of the core>, code:<reason>}`.
//
// Source of the code, in order:
//   1. `r.code` of the core (core/core.ts: today only `daily_limit_exceeded`);
//   2. the engine itself for `submitShift` plausibility (`minigame_too_short`, `impossible_score`: engine.ts, like the
//      reference server core/http/fakeApi.ts);
//   3. this table: the core reports a few rule violations only as Russian strings, mapped here to a stable code.
//      Messages are the core's own (sim/build.ts, sim/loan.ts, sim/economy.ts, sim/history.ts, core/core.ts); only the
//      ones that need a specific code are listed, the many "does not fit / occupied / loop ..." build errors stay `rejected`.
// The API turns `daily_limit_exceeded`, `minigame_too_short`, `impossible_score` into the top-level error code (422);
// every other reason is 422 `command_rejected` with `details.reason`.

export const REJECTION_REASONS = [
	'daily_limit_exceeded',
	'minigame_too_short',
	'impossible_score',
	'season_closed',
	'insufficient_funds',
	'not_found',
	'rejected'
] as const;
export type RejectionReason = (typeof REJECTION_REASONS)[number];

/** Generic fallback: every message that has no entry in RULES. */
export const GENERIC_REASON: RejectionReason = 'rejected';

/**
 * THE fallback table: message of the core -> reason. A string is an exact match, a RegExp is tested against the whole
 * message. The first matching row wins; no match = `rejected`.
 */
export const REJECTION_RULES: ReadonlyArray<readonly [pattern: string | RegExp, reason: RejectionReason]> = [
	// season
	['Сезон закрыт', 'season_closed'],
	// money ("Не хватает N 🪙" in build/upgrade/repair/expand/redo, undo refund, repayLoan with an empty purse)
	[/^Не хватает \d+ 🪙$/, 'insufficient_funds'],
	[/^Нужно \d+ 🪙, чтобы вернуть снесённое$/, 'insufficient_funds'],
	['Нет 🪙, чтобы вернуть', 'insufficient_funds'],
	// the referenced thing does not exist (any more)
	['Станция не найдена', 'not_found'],
	['Здание не найдено', 'not_found'],
	['Нет такого здания', 'not_found'],
	['Заказ недоступен', 'not_found'],
	['Нужна рампа отгрузки', 'not_found']
];

/** Reason for a rejection with the core's message `error` (used only when the core gave no `code`). */
export function rejectionCode(error: string | undefined): RejectionReason {
	if (typeof error !== 'string') return GENERIC_REASON;
	for (const [pattern, reason] of REJECTION_RULES) {
		if (typeof pattern === 'string' ? pattern === error : pattern.test(error)) return reason;
	}
	return GENERIC_REASON;
}
