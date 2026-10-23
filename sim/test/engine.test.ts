import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { OFFLINE_CAP_H } from '$lib/content/balance';
import { HISTORY_IN_STATE, accountedTo, bootstrap, catchUp, command, snapshot } from '../src/engine';
import type { StateObject } from '../src/host';
import { EXTRAS, H, PLAYER, T0, buildTutorialLine, newFactory, pickExtras, withExtras } from './helpers';

type S = Record<string, any>;
const s = (x: StateObject) => x as S;

describe('bootstrap', () => {
	it('null state creates a factory for the player with snapshot (player/factory/orders/season)', async () => {
		const r = await bootstrap(null, T0, PLAYER);
		const st = s(r.state);
		assert.equal(st.v, 1);
		assert.equal(st.player.id, 'u1');
		assert.equal(st.player.tgId, 1001);
		assert.equal(st.player.name, 'Ann');
		assert.equal(st.player.username, 'ann');
		assert.equal(st.player.ageConfirmed, false);
		assert.equal(r.snapshot.serverTime, T0);
		assert.equal(r.snapshot.player.id, 'u1');
		assert.equal(r.snapshot.factory.credits, 3000);
		assert.equal(r.snapshot.factory.layout.buildings.length, 0);
		assert.ok(r.snapshot.orders.length >= 1, 'the tutorial order is on the board');
		assert.equal(r.snapshot.season.id, 1);
		assert.equal(r.snapshot.season.closed, false);
		assert.equal(r.snapshot.season.startsAt, T0);
		// the clock is pinned to the request: no wall-clock leakage into the state
		assert.deepEqual(st.clock, { real: T0, server: T0, scale: 1 });
		assert.equal(st.lastTick, T0);
	});

	it('player without optional fields', async () => {
		const r = await bootstrap(null, T0, { id: '7', name: 'Bob' });
		assert.equal(s(r.state).player.id, '7');
		assert.ok(!('tgId' in s(r.state).player) && !('username' in s(r.state).player));
	});

	it('existing state: factory kept, time advances, player untouched', async () => {
		const st0 = await newFactory();
		const r = await bootstrap(st0, T0 + 60_000, { id: 'other', name: 'Other' });
		assert.equal(s(r.state).player.id, 'u1');
		assert.equal(s(r.state).lastTick, T0 + 60_000);
		assert.equal(r.snapshot.serverTime, T0 + 60_000);
	});

	it('is deterministic: same input, same output', async () => {
		const a = await bootstrap(null, T0, PLAYER);
		const b = await bootstrap(null, T0, PLAYER);
		assert.deepEqual(a, b);
	});

	it('rolls to the next season 1 h after the previous one closed (mock behaviour: bootstrap/advance only)', async () => {
		const st = await newFactory();
		s(st).season.endsAt = T0 + H;
		s(st).sim.stars = 12;
		const rolled = await bootstrap(st, T0 + 3 * H);
		assert.equal(rolled.snapshot.season.id, 2);
		assert.equal(rolled.snapshot.factory.stars, 0);
		const viaAdvance = await catchUp(st, T0 + 3 * H);
		assert.equal(viaAdvance.snapshot.season.id, 2);
		const viaCommand = await command(st, T0 + 3 * H, { type: 'ageConfirm' });
		assert.equal(viaCommand.snapshot.season.id, 1);
		assert.equal(viaCommand.snapshot.season.closed, true);
	});
});

describe('command', () => {
	it('renameFactory succeeds and changes state + snapshot', async () => {
		const st0 = await newFactory();
		const r = await command(st0, T0 + 5000, { type: 'renameFactory', name: '  Квас-1  ' });
		assert.equal(r.result.ok, true);
		assert.equal(s(r.state).factoryName, 'Квас-1');
		assert.equal(r.snapshot.factory.name, 'Квас-1');
		assert.equal(r.snapshot.serverTime, T0 + 5000);
		assert.equal(s(st0).factoryName, 'Мой цех', 'the input state object is not mutated');
		assert.ok(!('snapshot' in r.result));
	});

	it('rule rejection is ok:false with an error, not an exception', async () => {
		const st0 = await newFactory();
		const r = await command(st0, T0 + 5000, { type: 'renameFactory', name: 'x' });
		assert.equal(r.result.ok, false);
		assert.ok(r.result.error);
		assert.equal(s(r.state).factoryName, 'Мой цех');
		assert.equal(r.snapshot.factory.name, 'Мой цех');
		const bad = await command(st0, T0 + 5000, { type: 'acceptOrder', orderId: 'nope', dockId: 'b1' });
		assert.equal(bad.result.ok, false);
	});

	it('tick is rejected as tick_removed and does not touch the state', async () => {
		const st0 = withExtras(await newFactory());
		const before = structuredClone(st0);
		const r = await command(st0, T0 + 3 * H, { type: 'tick', from: T0, to: T0 + 1000, events: [] });
		assert.deepEqual(r.result, { ok: false, error: 'tick_removed', code: 'tick_removed' });
		assert.deepEqual(r.state, before, 'no catch-up on a removed command');
		assert.equal(r.snapshot.serverTime, T0 + 3 * H);
	});

	it('undo/redo are reported as history_unavailable until history is migrated into the state', async () => {
		const st0 = await newFactory();
		for (const type of ['undo', 'redo'] as const) {
			const r = await command(st0, T0 + 1000, { type });
			assert.deepEqual(r.result, { ok: false, error: 'history_unavailable', code: 'history_unavailable' });
		}
	});

	it('HISTORY_IN_STATE (the /health flag behind the API features.undo) mirrors the undo/redo rejection', async () => {
		const rejected = (await command(await newFactory(), T0 + 1000, { type: 'undo' })).result.ok === false;
		assert.equal(HISTORY_IN_STATE, !rejected, 'false while undo is rejected, true once S1U serves it');
	});

	it('real build commands work: the whole tutorial line is placed and the first order is served', async () => {
		const { state, now } = await buildTutorialLine(await newFactory(), T0);
		const layout = s(state).sim.layout;
		assert.deepEqual(
			layout.buildings.map((b: S) => b.kind).sort(),
			['blow', 'brew_tank', 'capper', 'dock', 'filler', 'intake', 'labeler', 'packer']
		);
		assert.ok(layout.conveyors.length > 0);
		const dock = layout.buildings.find((b: S) => b.kind === 'dock');
		const order = s(state).sim.orders[0];
		const acc = await command(state, now + 1000, { type: 'acceptOrder', orderId: order.id, dockId: dock.id });
		assert.equal(acc.result.ok, true, acc.result.error);
		assert.equal(acc.snapshot.orders.find((o) => o.id === order.id)!.status, 'loading');
		// a few hours later the truck is loaded and the order is paid out
		const done = await catchUp(acc.state, now + 1000 + 2 * H);
		const o = done.snapshot.orders.find((q) => q.id === order.id)!;
		assert.equal(o.status, 'done');
		assert.ok(done.snapshot.factory.xp > 0);
		assert.ok(done.snapshot.factory.shipped.kvas_ochakovsky_15! > 0);
	});

	it('a command after a long idle gap goes through the capped offline catch-up first', async () => {
		const { state, now } = await buildTutorialLine(await newFactory(), T0);
		const r = await command(state, now + 30 * H, { type: 'ageConfirm' });
		assert.equal(r.result.ok, true);
		assert.equal(r.snapshot.offline?.capped, true);
		assert.equal(r.snapshot.offline?.hours, OFFLINE_CAP_H);
		assert.equal(s(r.state).lastTick, now + 30 * H);
	});

	it('a command within the cap does not produce an offline report (plain mock behaviour)', async () => {
		const { state, now } = await buildTutorialLine(await newFactory(), T0);
		const r = await command(state, now + 2 * H, { type: 'ageConfirm' });
		assert.equal(r.snapshot.offline, undefined);
		assert.ok(r.snapshot.factory.produced! > 0);
	});
});

describe('unknown top-level fields round-trip untouched', () => {
	const check = (label: string, out: StateObject) => assert.deepEqual(pickExtras(out), EXTRAS, label);

	it('bootstrap (existing state)', async () => {
		const st = withExtras(await newFactory());
		check('bootstrap', (await bootstrap(st, T0 + 10 * 60_000)).state);
	});
	it('command: accepted, rejected, tick_removed, undo', async () => {
		const st = withExtras((await buildTutorialLine(await newFactory(), T0)).state);
		const now = T0 + 5 * H;
		check('ok', (await command(st, now, { type: 'renameFactory', name: 'Zavod' })).state);
		check('rejected', (await command(st, now, { type: 'renameFactory', name: 'x' })).state);
		check('tick', (await command(st, now, { type: 'tick', from: 0, to: 1, events: [] })).state);
		check('undo', (await command(st, now, { type: 'undo' })).state);
		check('after long idle', (await command(st, now + 40 * H, { type: 'ageConfirm' })).state);
	});
	it('advance: short, +3 h, capped, beyond the cap, capHours=0', async () => {
		const st = withExtras((await buildTutorialLine(await newFactory(), T0)).state);
		const now = T0 + 60_000;
		for (const [dh, cap] of [[0.01, undefined], [3, undefined], [12, undefined], [5, 1], [5, 0], [30, 2]] as const) {
			check(`advance ${dh}h cap=${cap}`, (await catchUp(st, now + dh * H, cap)).state);
		}
	});
	it('nested unknown fields inside known objects are kept as well', async () => {
		const st = await newFactory();
		s(st).sim.futureCounter = { n: 3 };
		s(st).player.flags = ['a'];
		const r = await command(st, T0 + 1000, { type: 'ageConfirm' });
		assert.deepEqual(s(r.state).sim.futureCounter, { n: 3 });
		assert.deepEqual(s(r.state).player.flags, ['a']);
	});
});

describe('advance', () => {
	it('+3 h of server time produces bottles, spends credits and moves lastTick', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const before = await snapshot(built.state, built.now);
		const t = built.now + 3 * H;
		const r = await catchUp(built.state, t);
		assert.equal(r.advancedMs, 3 * H);
		assert.equal(s(r.state).lastTick, t);
		assert.equal(r.snapshot.serverTime, t);
		assert.ok(r.snapshot.factory.produced! > before.factory.produced!);
		assert.ok(r.snapshot.factory.credits < before.factory.credits);
		assert.ok(Object.values(r.snapshot.factory.stock ?? {}).some((e) => e && e.boxes > 0));
		assert.ok(r.snapshot.offline && r.snapshot.offline.hours === 3 && r.snapshot.offline.capped === false);
		assert.equal(accountedTo(r.state), t);
	});

	it('is idempotent in time: advancing to the same moment again changes nothing', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const t = built.now + 3 * H;
		const a = await catchUp(built.state, t);
		const b = await catchUp(a.state, t);
		assert.equal(b.advancedMs, 0);
		assert.deepEqual(b.state, a.state);
	});

	it('past the 8 h cap: production stops after 8 h, report says capped', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const a = await catchUp(built.state, built.now + 8 * H);
		const b = await catchUp(built.state, built.now + 40 * H);
		assert.equal(b.advancedMs, 40 * H);
		assert.equal(b.snapshot.offline?.capped, true);
		assert.equal(b.snapshot.offline?.hours, 8);
		assert.equal(b.snapshot.factory.produced, a.snapshot.factory.produced);
		assert.equal(s(b.state).lastTick, built.now + 40 * H);
	});

	it('capHours lowers the cap: same output as advancing exactly capHours, still accounts the whole interval', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const plain = await catchUp(built.state, built.now + 1 * H);
		const capped = await catchUp(built.state, built.now + 5 * H, 1);
		assert.equal(capped.advancedMs, 5 * H);
		assert.ok(plain.snapshot.factory.produced! > 1000, 'an hour of production is not nothing');
		assert.equal(s(capped.state).lastTick, built.now + 5 * H);
		assert.equal(accountedTo(capped.state), built.now + 5 * H);
		assert.equal(capped.snapshot.factory.produced, plain.snapshot.factory.produced);
		assert.equal(capped.snapshot.factory.credits, plain.snapshot.factory.credits);
		assert.deepEqual(capped.snapshot.factory.stock, plain.snapshot.factory.stock);
		assert.deepEqual(capped.snapshot.factory.layout.buildings.map((b) => b.wear), plain.snapshot.factory.layout.buildings.map((b) => b.wear));
		assert.equal(capped.snapshot.offline?.hours, 1);
		assert.equal(capped.snapshot.offline?.capped, true);
		assert.equal(capped.snapshot.serverTime, built.now + 5 * H);
	});

	it('capHours=0 halts the factory completely; capHours above the mock cap is clamped to it', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const p0 = (await snapshot(built.state, built.now)).factory.produced;
		const halted = await catchUp(built.state, built.now + 4 * H, 0);
		assert.equal(halted.snapshot.factory.produced, p0);
		assert.equal(s(halted.state).lastTick, built.now + 4 * H);
		const huge = await catchUp(built.state, built.now + 30 * H, 1000);
		const std = await catchUp(built.state, built.now + 30 * H);
		assert.deepEqual(huge.state, std.state);
	});

	it('a gap that fits into capHours is a plain catch-up', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const a = await catchUp(built.state, built.now + 2 * H, 6);
		const b = await catchUp(built.state, built.now + 2 * H);
		assert.deepEqual(a.state, b.state);
	});

	it('the halted part still burns accepted orders whose deadline passes', async () => {
		let { state, now } = await buildTutorialLine(await newFactory(), T0);
		// open the order board and take a timed (non-tutorial) order on the dock
		const st = s(state);
		st.dialogs.done.push('q_dock');
		st.sim.collection.kvas_ochakovsky_15 = now; // tutorial is over
		st.sim.orders = [];
		now += 60_000;
		state = (await catchUp(state, now)).state;
		const avail = s(state).sim.orders.find((o: S) => o.status === 'available');
		assert.ok(avail, 'a timed order was generated');
		const dock = s(state).sim.layout.buildings.find((b: S) => b.kind === 'dock');
		// make it impossible to complete so it must expire
		avail.qty = 100000;
		const acc = await command(state, now + 1000, { type: 'acceptOrder', orderId: avail.id, dockId: dock.id });
		assert.equal(acc.result.ok, true, acc.result.error);
		// 1 h of production allowed, then halted: the order's deadline (<= 8 h after accept) passes inside the halted window
		const oneHour = await catchUp(acc.state, now + 1000 + 1 * H);
		const loadedAfterOneHour = oneHour.snapshot.orders.find((q) => q.id === avail.id)!;
		assert.equal(loadedAfterOneHour.status, 'loading');
		assert.ok(loadedAfterOneHour.loaded > 0);
		const r = await catchUp(acc.state, now + 1000 + 10 * H, 1);
		const o = r.snapshot.orders.find((q) => q.id === avail.id)!;
		assert.equal(o.status, 'failed', 'deadline burned the order although nothing was produced after the cap');
		assert.ok(Math.abs(o.loaded - loadedAfterOneHour.loaded) < 1e-6, 'nothing was loaded after the capped hour');
	});

	it('time going backwards is a no-op catch-up (advancedMs = 0)', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const r = await catchUp(built.state, built.now - 5000);
		assert.equal(r.advancedMs, 0);
		assert.equal(s(r.state).lastTick, built.now);
	});
});

describe('snapshot', () => {
	it('is a pure read: no catch-up, equal snapshots, input untouched', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const before = structuredClone(built.state);
		const t = built.now + 2 * H;
		const a = await snapshot(built.state, t);
		const b = await snapshot(built.state, t);
		assert.deepEqual(a, b);
		assert.deepEqual(built.state, before);
		assert.equal(a.serverTime, t);
		assert.equal(a.factory.lastTick, built.now, 'no lazy catch-up on a read');
	});
});

describe('isolation', () => {
	it('concurrent calls with different states and times do not leak into each other', async () => {
		const players = Array.from({ length: 24 }, (_, i) => ({ id: `p${i}`, tgId: 5000 + i, name: `Player${i}` }));
		const results = await Promise.all(
			players.map(async (p, i) => {
				const now = T0 + i * 7 * 60_000;
				const b = await bootstrap(null, now, p);
				const c = await command(b.state, now + 1000 * (i + 1), { type: 'renameFactory', name: `Zavod ${i}` });
				const a = await catchUp(c.state, now + (i + 1) * H);
				return { p, i, now, b, c, a };
			})
		);
		for (const { p, i, now, b, c, a } of results) {
			assert.equal(s(b.state).player.id, p.id);
			assert.equal(b.snapshot.player.name, p.name);
			assert.equal(b.snapshot.serverTime, now);
			assert.equal(c.snapshot.factory.name, `Zavod ${i}`);
			assert.equal(c.snapshot.player.id, p.id);
			assert.equal(a.snapshot.factory.name, `Zavod ${i}`);
			assert.equal(a.snapshot.serverTime, now + (i + 1) * H);
			assert.equal(s(a.state).lastTick, now + (i + 1) * H);
		}
	});

	it('globals are restored after success and after a crash inside the mock', async () => {
		const realNow = Date.now;
		const st = await newFactory();
		assert.equal(typeof (globalThis as S).localStorage, 'undefined');
		assert.equal(Date.now, realNow);
		await assert.rejects(command(st, T0 + 1000, { type: 'place' } as never));
		assert.equal(typeof (globalThis as S).localStorage, 'undefined');
		assert.equal(Date.now, realNow);
		assert.ok(Math.abs(Date.now() - T0) > 1000, 'Date.now is the wall clock again');
	});
});
