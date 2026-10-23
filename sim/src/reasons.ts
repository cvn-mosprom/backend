// Rejection reason codes (docs/ARCHITECTURE.md, "S2 decisions"): the mock reports rule violations only as Russian
// strings; `/v1/command` adds a stable machine code next to the original text: `{ok:false, error:<ru>, code:<reason>}`.

import type { Command } from '$lib/api/types';

export const REJECTION_REASONS = [
	'daily_limit_exceeded',
	'minigame_too_short',
	'impossible_score',
	'history_unavailable',
	'tick_removed',
	'season_closed',
	'insufficient_funds',
	'not_found',
	'rejected'
] as const;
export type RejectionReason = (typeof REJECTION_REASONS)[number];

/** Generic fallback: every message that has no entry in RULES. */
export const GENERIC_REASON: RejectionReason = 'rejected';

type Reason = RejectionReason | ((cmd?: Command) => RejectionReason);

/**
 * `submitShift` anti-cheat (MockGameApi.shift): one message, two causes. The cmd is inspected in the order of the
 * mock's own check (`durationMs < 400 || score < 0 || score > 1`).
 */
const shiftRejected = (cmd?: Command): RejectionReason => {
	if (cmd?.type !== 'submitShift') return GENERIC_REASON;
	const r = cmd.result;
	if (typeof r?.durationMs === 'number' && r.durationMs < 400) return 'minigame_too_short';
	if (typeof r?.score === 'number' && (r.score < 0 || r.score > 1)) return 'impossible_score';
	return GENERIC_REASON;
};

/**
 * THE mapping table: Russian message of the mock -> reason. A string is an exact match, a RegExp is tested against the
 * whole message. The first matching row wins; no match = `rejected`. Sources: vendor/frontend/web/src/lib
 * (api/mock/MockGameApi.ts, sim/build.ts, sim/loan.ts, sim/economy.ts, sim/history.ts); only the messages that need a
 * specific code are listed, the many "does not fit / occupied / loop ..." build errors are plain `rejected`.
 */
export const REJECTION_RULES: ReadonlyArray<readonly [pattern: string | RegExp, reason: Reason]> = [
	// shifts
	['Смены на сегодня кончились — завтра 5 новых', 'daily_limit_exceeded'],
	['Смена не засчитана', shiftRejected],
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

/** Reason for a rejection of `cmd` with the mock's message `error`. Unknown messages are `rejected`. */
export function rejectionCode(error: string | undefined, cmd?: Command): RejectionReason {
	if (typeof error !== 'string') return GENERIC_REASON;
	for (const [pattern, reason] of REJECTION_RULES) {
		if (typeof pattern === 'string' ? pattern === error : pattern.test(error)) return typeof reason === 'function' ? reason(cmd) : reason;
	}
	return GENERIC_REASON;
}
