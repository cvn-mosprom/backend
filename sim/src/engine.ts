// Sim operations behind the HTTP API: bootstrap / command / advance / snapshot.
// Everything is a pure function of (state, now, input) -> (state', output); no state is kept between calls.

import type { Command, CommandResult, GameSnapshot } from '$lib/api/types';
import { OFFLINE_CAP_H } from '$lib/content/balance';
import { advance as advanceSim } from '$lib/sim/advance';
import type { SimState } from '$lib/sim/economy';
import type { MockGameApi } from '$lib/api/mock/MockGameApi';
import { withMock, type StateObject } from './host';

const H = 3600_000;

export interface PlayerInfo {
	id: string;
	tgId?: number;
	name: string;
	username?: string;
}

export interface CommandOutcome {
	ok: boolean;
	error?: string;
	toast?: string;
	rewards?: { credits?: number; stars?: number; xp?: number; unlocked?: string[] };
}

/** The slice of MockState the engine reads or patches; everything else is opaque. */
interface StateView {
	lastTick: number;
	prodAt?: number;
	sim: SimState;
	season: { endsAt: number };
	offline?: { hours: number; capped: boolean };
}
const view = (s: StateObject) => s as unknown as StateView;

/** Server moment up to which the factory has been accounted: `min(prodAt ?? lastTick, lastTick)` (MockGameApi.tick). */
export const accountedTo = (s: StateObject): number => {
	const v = view(s);
	return Math.min(v.prodAt ?? v.lastTick, v.lastTick);
};

/** Commands that exist in the `Command` union but are not served by the sim. */
const REJECTED_COMMANDS: Record<string, string> = {
	// `tick` (client discrete simulation) is removed from the contract; the server is purely analytic.
	tick: 'tick_removed',
	// Undo history lives in MockGameApi.hist (memory only) and cannot survive between stateless requests until it is
	// migrated into the state (spec: principle 7). Remove these two entries when the migration lands.
	undo: 'history_unavailable',
	redo: 'history_unavailable'
};

/** Pure read of the snapshot at `now`: no catch-up, nothing persisted. */
export async function snapshot(state: StateObject, now: number): Promise<GameSnapshot> {
	const { value } = await withMock(state, now, (mock) => (mock as unknown as { snap(): GameSnapshot }).snap());
	return value;
}

/** Fresh factory for `player`, or the lazy 8 h offline catch-up of an existing one (MockGameApi.bootstrap). */
export async function bootstrap(
	state: StateObject | null,
	now: number,
	player?: PlayerInfo
): Promise<{ state: StateObject; snapshot: GameSnapshot }> {
	if (state) {
		const r = await catchUp(state, now, OFFLINE_CAP_H);
		return { state: r.state, snapshot: r.snapshot };
	}
	if (!player) throw new Error('player is required to create a factory');
	const { value, state: created } = await withMock(null, now, async (mock) => {
		const p: Record<string, unknown> = { id: player.id, name: player.name };
		if (player.tgId !== undefined) p.tgId = player.tgId;
		if (player.username !== undefined) p.username = player.username;
		mock.setPlayer(p);
		return mock.bootstrap();
	});
	return { state: created, snapshot: value };
}

/**
 * Lazy catch-up to `now` (the mock's bootstrap path: tick(offlineAllowed) + season roll + snapshot).
 *
 * Offline cap: the mock produces for at most OFFLINE_CAP_H hours after the last accounted moment and keeps the factory
 * halted (time passes, orders burn, no output) for the rest. `capHours` can only lower that:
 *   - capHours >= OFFLINE_CAP_H, or the gap fits into the cap: plain mock bootstrap;
 *   - otherwise three steps that reproduce the mock's own "produce, then halted" split at `from + capHours`:
 *       1. mock bootstrap at `from + capHours`  (full production for the capped window + offline report),
 *       2. `advance(sim, from + capHours, now, { halted: true })` from the vendored lib (orders burn, no output),
 *       3. mock bootstrap at `now` (gap is 0: day roll, orders refill, level-up, season roll, snapshot).
 *     The offline report of step 1 is patched to `hours = capHours, capped = true`.
 * `advancedMs` is the whole accounted interval `max(0, now - from)`, including the halted part.
 */
export async function catchUp(
	state: StateObject,
	now: number,
	capHours: number = OFFLINE_CAP_H
): Promise<{ state: StateObject; snapshot: GameSnapshot; advancedMs: number }> {
	const from = accountedTo(state);
	const advancedMs = Math.max(0, now - from);
	const cap = Number.isFinite(capHours) ? Math.min(Math.max(capHours, 0), OFFLINE_CAP_H) : OFFLINE_CAP_H;
	const capMs = cap * H;

	if (cap >= OFFLINE_CAP_H || advancedMs <= capMs) {
		const { value, state: out } = await withMock(state, now, (mock) => mock.bootstrap());
		return { state: out, snapshot: value, advancedMs };
	}

	// ---- capped below the mock's own cap ----
	const t1 = from + capMs;
	let mid: StateObject = state;
	const prevOffline = (state as { offline?: unknown }).offline;
	if (capMs > 0) {
		// detect a report produced by this catch-up: start from a state without one
		const clean: StateObject = { ...state };
		delete clean.offline;
		mid = (await withMock(clean, t1, (mock) => mock.bootstrap())).state;
		const v = view(mid);
		if (v.offline) {
			v.offline.hours = cap;
			v.offline.capped = true;
		} else if (prevOffline !== undefined) {
			(mid as { offline?: unknown }).offline = prevOffline;
		}
	} else {
		mid = structuredClone(state);
	}
	// the factory stands still for the rest of the interval: time passes, deadlines burn, nothing is produced
	const m = view(mid);
	advanceSim(m.sim, Math.max(t1, from), now, { halted: true, seasonOpen: now < m.season.endsAt });
	m.lastTick = now;
	m.prodAt = now;
	const { value, state: out } = await withMock(mid, now, (mock) => mock.bootstrap());
	return { state: out, snapshot: value, advancedMs };
}

/** Apply a player command (MockGameApi.command: lazy catch-up, apply, level-up). */
export async function command(
	state: StateObject,
	now: number,
	cmd: Command
): Promise<{ state: StateObject; result: CommandOutcome; snapshot: GameSnapshot }> {
	const reason = Object.hasOwn(REJECTED_COMMANDS, cmd.type) ? REJECTED_COMMANDS[cmd.type] : undefined;
	if (reason) return { state, result: { ok: false, error: reason }, snapshot: await snapshot(state, now) };

	// The mock's command() catches up with tick(false), which is NOT capped (it is meant for a client that was just
	// bootstrapped). A command arriving after a long idle gap must not turn into an uncapped offline catch-up, so a
	// gap beyond the cap goes through the capped path first (offline report included).
	let s = state;
	if (now - accountedTo(s) > OFFLINE_CAP_H * H) s = (await catchUp(s, now, OFFLINE_CAP_H)).state;

	const { value, state: out } = await withMock(s, now, (mock: MockGameApi) => mock.command(cmd));
	const { snapshot: snap, ...result } = value as CommandResult & { snapshot?: GameSnapshot };
	return { state: out, result: result as CommandOutcome, snapshot: snap as GameSnapshot };
}

/** Commands the sim understands (the `Command` union; `tick` is listed so it can be answered with `tick_removed`). */
export const COMMAND_TYPES: ReadonlySet<string> = new Set<Command['type']>([
	'ageConfirm', 'renameFactory', 'setUniversity', 'place', 'placeConveyor', 'rerouteConveyor', 'remove', 'move', 'rotate',
	'configure', 'upgrade', 'upgradeConveyor', 'repair', 'expandHangar', 'paint', 'acceptOrder', 'cancelOrder', 'sellMarket',
	'takeLoan', 'repayLoan', 'submitShift', 'dialogDone', 'factSeen', 'undo', 'redo', 'buildEnd', 'offlineSeen', 'tick'
]);
