// Shift limit truth: the daily counter lives in the persisted state (top-level shiftsLeft / shiftsDay), nowhere else.

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import type { Command } from '$lib/api/types';
import { applyCommand, migrateState } from '$lib/core';
import { bootstrap, catchUp, command, effectiveNow, snapshot, type StateObject } from '../src/engine';
import { buildTutorialLine, newFactory, shifts, shiftCmd } from './helpers';

const MIN = 60_000;
/** 2026-10-24 23:40 MSK: 20 minutes before the next MSK midnight (00:00 UTC+3 = 21:00 UTC). */
const NIGHT = Date.parse('2026-10-24T20:40:00Z');
const MIDNIGHT = Date.parse('2026-10-24T21:00:00Z');

let built: { state: StateObject; now: number };
before(async () => {
	built = await buildTutorialLine(await newFactory(NIGHT), NIGHT);
	assert.ok(built.now < MIDNIGHT - 5 * MIN);
});

/** 5 valid shifts, one per second, chaining the returned state. */
async function burnShifts(state: StateObject, now: number, n = 5) {
	for (let i = 0; i < n; i++) {
		now += 1000;
		const r = await command(state, now, shiftCmd(state));
		assert.equal(r.result.ok, true, `shift ${i + 1}: ${r.result.error}`);
		assert.equal(r.snapshot.season.shiftsLeft, 5 - (i + 1), 'snapshot shows the remaining shifts');
		state = r.state;
	}
	return { state, now };
}

describe('shift limit lives in the persisted state', () => {
	it('5 valid shifts pass, the 6th is daily_limit_exceeded; the counter is in the state', async () => {
		assert.equal(shifts(built.state).shiftsLeft, 5);
		const s5 = await burnShifts(built.state, built.now);
		assert.equal(shifts(s5.state).shiftsLeft, 0);
		assert.equal(shifts(s5.state).shiftsDay, shifts(built.state).shiftsDay);

		const sixth = await command(s5.state, s5.now + 1000, shiftCmd(s5.state));
		assert.deepEqual(sixth.result, { ok: false, error: 'Смены на сегодня кончились — завтра 5 новых', code: 'daily_limit_exceeded' });
		assert.equal(shifts(sixth.state).shiftsLeft, 0);
		assert.equal(sixth.snapshot.season.shiftsLeft, 0);
	});

	it('the counter survives a JSON round trip (DB), and the input state is never mutated (no hidden counter)', async () => {
		const before = structuredClone(built.state);
		const s5 = await burnShifts(built.state, built.now);
		assert.deepEqual(built.state, before, 'input state untouched');
		assert.equal(shifts(built.state).shiftsLeft, 5, 'replaying from the old state starts from 5 again: the state is the only truth');
		const reloaded = JSON.parse(JSON.stringify(s5.state)) as StateObject;
		const r = await command(reloaded, s5.now + 1000, shiftCmd(reloaded));
		assert.equal(r.result.code, 'daily_limit_exceeded');
	});

	it('rejected shifts (too short, bad score, unknown station) do not consume the counter', async () => {
		let st = built.state;
		for (const cmd of [shiftCmd(st, { durationMs: 399 }), shiftCmd(st, { score: 2 }), shiftCmd(st, {}, 'nope')] as Command[]) {
			const r = await command(st, built.now + 1000, cmd);
			assert.equal(r.result.ok, false);
			st = r.state;
		}
		assert.equal(shifts(st).shiftsLeft, 5);
	});

	it('after the next MSK midnight the player has 5 shifts again (and not before it)', async () => {
		const s5 = await burnShifts(built.state, built.now);
		// one second before midnight: still the old day
		const before = await command(s5.state, MIDNIGHT - 1000, shiftCmd(s5.state));
		assert.equal(before.result.code, 'daily_limit_exceeded');
		assert.equal(shifts(before.state).shiftsLeft, 0);
		// exactly at / after midnight: new MSK day
		for (const t of [MIDNIGHT, MIDNIGHT + 30 * 1000]) {
			const r = await command(s5.state, t, shiftCmd(s5.state));
			assert.equal(r.result.ok, true, `at ${new Date(t).toISOString()}: ${r.result.error}`);
			assert.equal(shifts(r.state).shiftsLeft, 4);
			assert.equal(shifts(r.state).shiftsDay, '2026-10-25');
		}
		const day2 = await burnShifts(s5.state, MIDNIGHT + MIN - 5000, 5);
		assert.equal(shifts(day2.state).shiftsLeft, 0);
		assert.equal((await command(day2.state, day2.now + 1000, shiftCmd(day2.state))).result.code, 'daily_limit_exceeded');
	});

	it('advance / bootstrap past midnight also roll the day (state is persisted by them too)', async () => {
		const s5 = await burnShifts(built.state, built.now);
		const adv = await catchUp(s5.state, MIDNIGHT + 2 * MIN);
		assert.equal(shifts(adv.state).shiftsLeft, 5);
		assert.equal(adv.snapshot.season.shiftsLeft, 5);
		const boot = await bootstrap(s5.state, MIDNIGHT + 2 * MIN);
		assert.equal(shifts(boot.state).shiftsLeft, 5);
	});
});

describe('time never runs backwards: effective now = max(now, state.lastTick)', () => {
	it('effectiveNow', () => {
		const lt = (built.state as { lastTick: number }).lastTick;
		assert.equal(effectiveNow(built.state, lt - 1e9), lt);
		assert.equal(effectiveNow(built.state, lt + 5), lt + 5);
	});

	it('going back across midnight does NOT re-grant shifts (command, advance, bootstrap, snapshot)', async () => {
		// day 2 (after midnight), all five shifts used
		const start = await catchUp(built.state, MIDNIGHT + 2 * MIN);
		const used = await burnShifts(start.state, MIDNIGHT + 2 * MIN);
		const st = used.state;
		assert.equal(shifts(st).shiftsDay, '2026-10-25');
		assert.equal(shifts(st).shiftsLeft, 0);
		const lastTick = (st as { lastTick: number }).lastTick;
		const yesterday = MIDNIGHT - 10 * MIN; // before the midnight that already passed

		// the failure mode this guards: the raw core re-rolls the day when asked at an earlier day
		const raw = applyCommand(migrateState(structuredClone(st)), shiftCmd(st), yesterday);
		assert.equal(raw.ok, true, 'unguarded core grants a fresh shift when the clock goes back over midnight');

		// guarded entry points
		const r = await command(st, yesterday, shiftCmd(st));
		assert.equal(r.result.code, 'daily_limit_exceeded');
		assert.equal(shifts(r.state).shiftsLeft, 0);
		assert.equal(shifts(r.state).shiftsDay, '2026-10-25');
		assert.equal((r.state as { lastTick: number }).lastTick, lastTick, 'lastTick never moves back');
		assert.equal(r.snapshot.serverTime, lastTick, 'the snapshot is served at the effective time');

		const adv = await catchUp(st, yesterday);
		assert.equal(adv.advancedMs, 0);
		assert.equal(shifts(adv.state).shiftsLeft, 0);
		const boot = await bootstrap(st, yesterday);
		assert.equal(shifts(boot.state).shiftsLeft, 0);
		assert.equal((await snapshot(st, yesterday)).season.shiftsLeft, 0);

		// back and forth: still nothing re-granted, and the forward jump later is a plain catch-up
		const back = await command(r.state, yesterday - MIN, shiftCmd(r.state));
		assert.equal(back.result.code, 'daily_limit_exceeded');
		const fwd = await command(back.state, lastTick + 1000, shiftCmd(back.state));
		assert.equal(fwd.result.code, 'daily_limit_exceeded');
	});

	it('a backwards command is otherwise served at lastTick (applied, version-neutral time)', async () => {
		const lt = (built.state as { lastTick: number }).lastTick;
		const r = await command(built.state, lt - 3 * 3600_000, { type: 'renameFactory', name: 'Назад' });
		assert.equal(r.result.ok, true);
		assert.equal((r.state as { lastTick: number }).lastTick, lt);
		assert.equal(r.snapshot.serverTime, lt);
	});
});
