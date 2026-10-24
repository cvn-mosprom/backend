// Sim operations behind the HTTP API: bootstrap / command / advance / snapshot.
// Everything is a pure function of (state, now, input) -> (state', output); no state is kept between calls.
//
// The game itself is `$lib/core` of the vendored frontend (vendor/frontend/web/src/lib/core/README.md): the same pure
// functions the frontend's reference server runs (core/http/fakeApi.ts), so client and server count bit for bit.
// This module is the thin wrapper the HTTP API needs around it:
//   - every state is loaded through `migrateState` (lazily, on a copy; the API persists the migrated state with its next
//     write, there is no bulk migration), unknown top-level fields (`meta.idempotency`, `meta.speed`, ...) ride along;
//   - time is a parameter, and `max(now, state.lastTick)` is applied by EVERY entry point (the factory clock never runs
//     backwards: a request stamped before `lastTick` is served as if it arrived at `lastTick`; the shift counter would
//     otherwise be re-granted whenever a caller's clock crosses MSK midnight in the wrong direction);
//   - the offline report of a `background` advance accumulates instead of being replaced (the core replaces);
//   - `submitShift` plausibility (minigame_too_short / impossible_score) is decided here, before the core, like the
//     reference server does; the other rejection codes come from the core's `r.code` (reasons.ts keeps a text fallback).

import type { Command, GameSnapshot, OfflineReport } from '$lib/api/types';
import { OFFLINE_CAP_H } from '$lib/content/balance';
import { STATE_V, advanceState, applyCommand, freshState, migrateState, snapshotOf, type FactoryState, type Rewards } from '$lib/core';
import { rejectionCode } from './reasons';

const H = 3600_000;
/** Shortest believable minigame round (core/core.ts `shift`: `durationMs < 400`). */
const SHIFT_MIN_MS = 400;

/** Persisted factory state as the API stores it: the core's `FactoryState` plus unknown top-level fields. */
export type StateObject = Record<string, unknown>;

export interface PlayerInfo {
	id: string;
	tgId?: number;
	name: string;
	username?: string;
	/** First-touch source (start_param); only used when the factory is created. */
	source?: string;
}

export interface CommandOutcome {
	ok: boolean;
	/** Rejections only: the core's original (Russian) message. */
	error?: string;
	/** Rejections only: stable machine reason (core `r.code`, else reasons.ts). */
	code?: string;
	toast?: string;
	rewards?: Rewards;
}

/** The stored state cannot be brought to the current schema (corrupt, or newer than this core). The API sent garbage: 400. */
export class StateError extends Error {}

/** Commands the sim understands: the `Command` union of the vendored types.ts (`tick` left it in frontend revision 4). */
export const COMMAND_TYPES: ReadonlySet<string> = new Set<Command['type']>([
	'ageConfirm', 'renameFactory', 'setUniversity', 'place', 'placeConveyor', 'rerouteConveyor', 'remove', 'move', 'rotate',
	'configure', 'upgrade', 'upgradeConveyor', 'repair', 'expandHangar', 'paint', 'acceptOrder', 'cancelOrder', 'sellMarket',
	'takeLoan', 'repayLoan', 'submitShift', 'dialogDone', 'factSeen', 'undo', 'redo', 'buildEnd', 'offlineSeen'
]);

/**
 * True: undo/redo are served from `state.history` (core/core.ts, sim/history.ts), which lives inside the state and
 * survives between stateless requests. Reported by `/health` as `historyInState`; the API turns it into `features.undo`.
 */
export const HISTORY_IN_STATE = true;

/** Schema version of the states this core writes (`FactoryState.v`). */
export const STATE_VERSION: number = STATE_V;

/**
 * Effective server time of a request: `max(now, state.lastTick)`. Applied by EVERY entry point (snapshot, bootstrap,
 * command, advance). The core takes `now` as given and would run a negative interval for an earlier one.
 */
export const effectiveNow = (s: object, now: number): number => Math.max(now, (s as { lastTick: number }).lastTick);

/**
 * A working copy of a stored state, migrated to the current schema (`migrateState`: v1 -> v2 moves the shift counter out of
 * `season`, adds `history`, upgrades a 12x10 hangar). The caller's object is never touched; the core then mutates the copy.
 * v1 leftovers of the old MockGameApi host that nothing reads any more (`clock`, `lastSeenReal`) are dropped; every other
 * unknown top-level field is kept as is.
 */
export function loadState(raw: StateObject): FactoryState {
	const s = structuredClone(raw);
	const was = typeof s.v === 'number' ? s.v : 1;
	let st: FactoryState;
	try {
		st = migrateState(s);
	} catch (e) {
		throw new StateError(e instanceof Error ? e.message : String(e));
	}
	if (was < STATE_V) {
		delete s.clock;
		delete s.lastSeenReal;
	}
	return st;
}

const out = (s: FactoryState): StateObject => s as unknown as StateObject;

/**
 * The snapshot the client gets. No leaderboard is passed: the API replaces `rank` with the real place in every response
 * (game.py `_set_rank`), and the core's own rank would rank the player against ~1300 generated bots on every call.
 */
const snap = (s: FactoryState, now: number): GameSnapshot => snapshotOf(s, now, { tab: 'general', rows: [], total: 0, inviteCutoff: 0 });

/** Pure read of the snapshot at `now`: no catch-up, nothing persisted. */
export function snapshot(state: StateObject, now: number): GameSnapshot {
	const s = loadState(state);
	return snap(s, effectiveNow(s, now));
}

/** Fresh factory for `player`, or the lazy offline catch-up (cap 8 h) of an existing one (fakeApi `bootstrap`). */
export function bootstrap(
	state: StateObject | null,
	now: number,
	player?: PlayerInfo
): { state: StateObject; snapshot: GameSnapshot } {
	let s: FactoryState;
	if (state) {
		s = loadState(state);
		now = effectiveNow(s, now);
	} else {
		if (!player) throw new Error('player is required to create a factory');
		const init: Parameters<typeof freshState>[0] = { id: player.id, name: player.name };
		if (player.tgId !== undefined) init.tgId = player.tgId;
		if (player.username !== undefined) init.username = player.username;
		if (player.source !== undefined) init.source = player.source;
		s = freshState(init, now);
	}
	advanceState(s, now, { offlineReport: true });
	return { state: out(s), snapshot: snap(s, now) };
}

/** `capHours` is the number of hours the factory works without the player; it can only lower the core's own 8 h. */
const clampCap = (capHours: number | undefined): number =>
	capHours === undefined || !Number.isFinite(capHours) ? OFFLINE_CAP_H : Math.min(Math.max(capHours, 0), OFFLINE_CAP_H);

/**
 * Lazy catch-up to `now` with an offline report: production for at most `capHours` after the last accounted moment, then
 * the factory stands still (time passes, orders burn, nothing is produced). `advancedMs` is the whole accounted interval
 * `max(0, now - lastTick)`, including the halted part.
 */
export function catchUp(
	state: StateObject,
	now: number,
	capHours?: number
): { state: StateObject; snapshot: GameSnapshot; advancedMs: number } {
	const s = loadState(state);
	now = effectiveNow(s, now);
	const advancedMs = now - s.lastTick;
	advanceState(s, now, { capHours: clampCap(capHours), offlineReport: true });
	return { state: out(s), snapshot: snap(s, now), advancedMs };
}

/** Why the caller advances the factory (`/v1/advance` `reason`). Anything but `background` is a plain catch-up. */
export type AdvanceReason = 'resume' | 'manual' | 'background';

/**
 * Fold a new offline report into the unseen one. Used when the player has not looked at the previous report yet, so the
 * summary still covers the whole absence: hours summed (capped at OFFLINE_CAP_H, `capped` set when the cap is hit or either
 * part was capped), produced bottles / boxes / spent credits summed, `broken` merged without duplicates, `storageFull` OR-ed.
 */
export function mergeOffline(prev: OfflineReport, cur: OfflineReport): OfflineReport {
	const hours = (prev.hours || 0) + (cur.hours || 0);
	const boxes: Record<string, number> = { ...prev.producedBoxes };
	for (const [sku, n] of Object.entries(cur.producedBoxes ?? {})) boxes[sku] = (boxes[sku] ?? 0) + n;
	return {
		hours: Math.min(hours, OFFLINE_CAP_H),
		capped: !!prev.capped || !!cur.capped || hours > OFFLINE_CAP_H,
		producedBottles: (prev.producedBottles || 0) + (cur.producedBottles || 0),
		producedBoxes: boxes,
		creditsSpent: (prev.creditsSpent || 0) + (cur.creditsSpent || 0),
		broken: [...new Set([...(prev.broken ?? []), ...(cur.broken ?? [])])],
		storageFull: !!prev.storageFull || !!cur.storageFull
	};
}

/**
 * `/v1/advance`: `catchUp` plus the report policy of `reason`.
 * - `background` (a lazy catch-up while the player is away): an unseen `state.offline` is never replaced by a smaller one,
 *   the report of this step is merged into it (`mergeOffline`). The step runs from a state without the old report so a
 *   report produced by the step itself can be told apart; without a new one the old report is put back untouched.
 *   The core replaces the report, so this stays in the wrapper.
 * - `resume` / `manual` / absent: plain catch-up (a new report replaces the old one).
 */
export function advance(
	state: StateObject,
	now: number,
	opts: { capHours?: number; reason?: AdvanceReason | string } = {}
): { state: StateObject; snapshot: GameSnapshot; advancedMs: number } {
	const s = loadState(state);
	now = effectiveNow(s, now);
	const advancedMs = now - s.lastTick;
	const prev = opts.reason === 'background' ? s.offline : undefined;
	if (prev) delete s.offline;
	advanceState(s, now, { capHours: clampCap(opts.capHours), offlineReport: true });
	if (prev) s.offline = s.offline ? mergeOffline(prev, s.offline) : prev;
	return { state: out(s), snapshot: snap(s, now), advancedMs };
}

type CoreOutcome = ReturnType<typeof applyCommand>;

/**
 * `submitShift` anti-cheat, in the order of the reference server (core/http/fakeApi.ts): a round shorter than 400 ms is
 * `minigame_too_short`, a score outside 0..1 is `impossible_score`. The core itself only says "Смена не засчитана".
 */
function shiftPlausibility(cmd: Command): CoreOutcome | undefined {
	if (cmd.type !== 'submitShift') return undefined;
	const r = cmd.result;
	if (!(typeof r?.durationMs === 'number' && r.durationMs >= SHIFT_MIN_MS)) {
		return { ok: false, error: 'Смена не засчитана: слишком быстро', code: 'minigame_too_short' };
	}
	if (!(typeof r.score === 'number' && r.score >= 0 && r.score <= 1)) {
		return { ok: false, error: 'Смена не засчитана', code: 'impossible_score' };
	}
	return undefined;
}

/**
 * Apply a player command (core `applyCommand`: lazy catch-up to `now`, the command, level-up, order schedule).
 * A rejection keeps the catch-up (the time has passed) and changes nothing else; the snapshot is the fresh one.
 */
export function command(
	state: StateObject,
	now: number,
	cmd: Command
): { state: StateObject; result: CommandOutcome; snapshot: GameSnapshot } {
	const s = loadState(state);
	now = effectiveNow(s, now);
	// A command arriving after a gap beyond the cap first goes through the capped catch-up WITH the offline report
	// (the player was away): the core's applyCommand catches up too, but silently.
	if (now - s.lastTick > OFFLINE_CAP_H * H && cmd.type !== 'offlineSeen') advanceState(s, now, { offlineReport: true });
	const refused = shiftPlausibility(cmd);
	if (refused) advanceState(s, now);
	const r = refused ?? applyCommand(s, cmd, now);
	const result: CommandOutcome = r.ok ? { ok: true } : { ok: false, error: r.error, code: r.code ?? rejectionCode(r.error) };
	if (r.ok) {
		if (r.toast !== undefined) result.toast = r.toast;
		if (r.rewards !== undefined) result.rewards = r.rewards;
	}
	return { state: out(s), result, snapshot: snap(s, now) };
}
