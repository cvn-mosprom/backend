// What the move from MockGameApi to $lib/core changed for the API: old (v1) states load without loss, undo/redo live in the
// state and survive JSON round trips, etaAt follows rebuilds, the state stays small.
//
// Fixtures (test/fixtures):
//   v1-states.json  — real states of the dev DB (e2e factories), dumped with
//                     `docker compose exec -T db psql -U mosprom -d mosprom -At -c "select state::text from factories where not is_bot"`
//                     (ids and names replaced). All v1, created by the old sim.
//   v1-played.json  — a factory played on the OLD sim over HTTP (tutorial line, two orders, shifts, a loan, an unseen offline
//                     report, a loading order) with the snapshot the old sim returned at `now`.

import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import Ajv from 'ajv';
import addFormats from 'ajv-formats';
import type { GameSnapshot } from '$lib/api/types';
import { UNDO_STEPS } from '$lib/content/balance';
import { STATE_VERSION, bootstrap, catchUp, command, loadState, snapshot, type StateObject } from '../src/engine';
import { H, T0, buildTutorialLine, shiftCmd, tutorialCommands } from './helpers';

type S = Record<string, any>;
const fixture = <T>(name: string): T => JSON.parse(readFileSync(fileURLToPath(new URL('./fixtures/' + name, import.meta.url)), 'utf8')) as T;
const dbStates = fixture<S[]>('v1-states.json');
const played = fixture<{ now: number; state: S; snapshot: GameSnapshot }>('v1-played.json');

const schemaPath = fileURLToPath(new URL('../../contract/schema/snapshot.schema.json', import.meta.url));
function validator() {
	if (!existsSync(schemaPath)) return () => undefined;
	const ajv = new Ajv({ allErrors: true, strict: false });
	addFormats(ajv);
	const validate = ajv.compile(JSON.parse(readFileSync(schemaPath, 'utf8')));
	return (snap: unknown, label: string) => assert.ok(validate(snap), `${label}: ${ajv.errorsText(validate.errors)}`);
}

/** Everything the migration must keep, key by key (the v1 -> v2 move only touches the shift counter, history and mock leftovers). */
const KEPT = ['layoutV', 'player', 'factoryName', 'level', 'sim', 'lastTick', 'skills', 'dialogs', 'ordersDay', 'offline', 'referrals', 'shiftsTotal', 'meta'];

describe('v1 states of the old sim (real DB dump) load through migrateState without loss', () => {
	it('the dump is what the old sim wrote: v1, shifts inside season, mock clock', () => {
		assert.ok(dbStates.length >= 4);
		for (const s of dbStates) {
			assert.equal(s.v, 1);
			assert.equal(typeof s.season.shiftsLeft, 'number');
			assert.ok(s.clock && 'lastSeenReal' in s && 'prodAt' in s);
		}
	});

	it('v2 shape: shift counter at the top level, history in the state, mock leftovers gone, everything else untouched', () => {
		for (const old of dbStates) {
			const before = structuredClone(old);
			const s = loadState(old) as unknown as S;
			assert.deepEqual(old, before, 'the stored object is never mutated');
			assert.equal(s.v, STATE_VERSION);
			assert.equal(s.shiftsLeft, old.season.shiftsLeft);
			assert.equal(s.shiftsDay, old.season.shiftsDay);
			assert.deepEqual(s.season, { id: old.season.id, startsAt: old.season.startsAt, endsAt: old.season.endsAt });
			assert.deepEqual(s.history, { undo: [], redo: [] });
			for (const k of ['clock', 'lastSeenReal', 'prodAt', 'discReal']) assert.ok(!(k in s), k);
			for (const k of KEPT) assert.deepEqual(s[k], old[k], k);
			// nothing else was invented or dropped at the top level
			const extra = Object.keys(s).filter((k) => !(k in old));
			assert.deepEqual(extra.sort(), ['history', 'shiftsDay', 'shiftsLeft'].sort());
		}
	});

	it('meta (idempotency log, speed baseline) is preserved byte for byte through every entry point', () => {
		for (const old of dbStates) {
			assert.ok(old.meta);
			const now = old.lastTick + 20 * 60_000;
			assert.deepEqual((bootstrap(old, now).state as S).meta, old.meta);
			assert.deepEqual((catchUp(old, now + 5 * H).state as S).meta, old.meta);
			assert.deepEqual((command(old, now, { type: 'ageConfirm' }).state as S).meta, old.meta);
		}
	});

	it('migration is idempotent and survives the JSON round trip the API puts it through', () => {
		const check = validator();
		for (const old of dbStates) {
			const first = bootstrap(old, old.lastTick + 60_000);
			const again = bootstrap(JSON.parse(JSON.stringify(first.state)), old.lastTick + 60_000);
			assert.deepEqual(again.state, first.state);
			check(first.snapshot, 'snapshot of a migrated v1 state');
		}
	});

	it('the factory is intact: layout, money, orders, skills, season, shift counter in the snapshot', () => {
		for (const old of dbStates) {
			const snap = snapshot(old, old.lastTick);
			assert.deepEqual(snap.factory.layout, old.sim.layout);
			assert.equal(snap.factory.credits, Math.floor(old.sim.credits));
			assert.equal(snap.factory.level, old.level);
			assert.equal(snap.factory.name, old.factoryName);
			assert.deepEqual(snap.skills, old.skills);
			assert.deepEqual(snap.dialogs, old.dialogs);
			assert.deepEqual(snap.offline, old.offline);
			assert.equal(snap.season.id, old.season.id);
			assert.equal(snap.season.shiftsLeft, old.season.shiftsLeft);
			assert.deepEqual(snap.orders.map((o) => o.id), old.sim.orders.map((o: S) => o.id));
		}
	});

	it('they keep playing: advance (short, +3 h, past the cap), commands, undo on an empty history — no exception, schema-valid', () => {
		const check = validator();
		for (const old of dbStates) {
			const t = old.lastTick;
			for (const dt of [1000, 3 * H, 30 * H]) check(catchUp(old, t + dt).snapshot, `advance +${dt}`);
			const r = command(old, t + 5000, { type: 'renameFactory', name: 'После миграции' });
			assert.equal(r.result.ok, true);
			assert.equal(r.snapshot.factory.name, 'После миграции');
			const u = command(old, t + 5000, { type: 'undo' });
			assert.equal(u.result.ok, false);
			assert.equal(u.result.error, 'Нечего отменять');
			check(u.snapshot, 'undo on empty history');
		}
	});
});

describe('a factory played on the old sim (loading order, loan, unseen offline report)', () => {
	it('the snapshot of the new core equals the one the old sim returned (everything the old one knew)', () => {
		const s = played.state;
		assert.equal(s.v, 1);
		const now = played.now;
		const neu = snapshot(s, now);
		const old = played.snapshot;
		const strip = (o: S) => ({ ...o, etaAt: undefined });
		assert.deepEqual(neu.orders.map(strip).map((o) => JSON.parse(JSON.stringify(o))), old.orders.map((o) => JSON.parse(JSON.stringify(strip(o)))));
		assert.deepEqual(neu.factory.layout, old.factory.layout);
		for (const k of ['credits', 'stars', 'xp', 'level', 'shipped', 'collection', 'stock', 'produced', 'lastShipped', 'loan', 'loanLog', 'warehouse', 'name'] as const) {
			assert.deepEqual(neu.factory[k], old.factory[k], k);
		}
		assert.deepEqual(neu.skills, old.skills);
		assert.deepEqual(neu.dialogs, old.dialogs);
		assert.deepEqual(neu.offline, old.offline);
		assert.deepEqual(neu.player, old.player);
		assert.deepEqual(neu.season, old.season, 'season block incl. shiftsLeft');
		assert.equal(neu.serverTime, now);
	});

	it('the migrated state carries on: the loading order gets etaAt and loads, shifts keep counting', () => {
		const now = played.now;
		const loading = (snap: GameSnapshot) => snap.orders.find((o) => o.status === 'loading')!;
		assert.ok(loading(played.snapshot), 'the fixture has a loading order');
		assert.equal(loading(played.snapshot).etaAt, undefined, 'the old sim knew no etaAt');

		const r = catchUp(played.state, now + 1000);
		const o = loading(r.snapshot);
		assert.equal(typeof o.etaAt, 'number', 'etaAt appears on the first catch-up of an old factory');
		assert.ok(o.etaAt! >= now + 1000);

		const later = catchUp(r.state, now + 2 * H);
		const done = later.snapshot.orders.find((q) => q.id === o.id)!;
		assert.ok(done.status === 'done' || done.loaded > o.loaded, 'the order moved on');

		const sh = command(r.state, now + 5000, shiftCmd(r.state));
		assert.equal(sh.result.ok, true, sh.result.error);
		assert.equal(sh.snapshot.season.shiftsLeft, played.snapshot.season.shiftsLeft - 1);
	});

	it('the first new build after migration is undoable, and the history is stored in the state', () => {
		const now = played.now + 1000;
		const painted = command(played.state, now, { type: 'paint', cells: [{ x: 3, y: 1 }], color: 'red' });
		assert.equal(painted.result.ok, true, painted.result.error);
		assert.equal(painted.snapshot.undoSteps, 1);
		assert.equal((painted.state as S).history.undo.length, 1);
		const reloaded = JSON.parse(JSON.stringify(painted.state)) as StateObject;
		const undone = command(reloaded, now + 1000, { type: 'undo' });
		assert.equal(undone.result.ok, true, undone.result.error);
		assert.deepEqual(undone.snapshot.factory.layout.hangar.paint, played.snapshot.factory.layout.hangar.paint);
	});
});

describe('undo / redo live in the state', () => {
	it('every step survives a JSON round trip (stateless requests); redo replays; a new action clears redo', () => {
		let state = newFactoryState();
		let now = T0;
		const steps: StateObject[] = [];
		for (const c of tutorialCommands().slice(0, 4)) {
			now += 1000;
			const r = command(state, now, c);
			assert.equal(r.result.ok, true, r.result.error);
			state = JSON.parse(JSON.stringify(r.state));
			steps.push(state);
		}
		const count = (st: StateObject) => (st as S).sim.layout.buildings.length + (st as S).sim.layout.conveyors.length;
		const full = count(state);
		let snap = snapshot(state, now);
		assert.equal(snap.undoSteps, 4);
		assert.ok(snap.undoLabel);

		for (let i = 3; i >= 0; i--) {
			now += 1000;
			const r = command(state, now, { type: 'undo' });
			assert.equal(r.result.ok, true, r.result.error);
			state = JSON.parse(JSON.stringify(r.state));
			assert.equal(r.snapshot.undoSteps, i);
			assert.equal(r.snapshot.redoSteps, 4 - i);
		}
		assert.equal(count(state), 0);
		assert.equal(command(state, now + 1000, { type: 'undo' }).result.ok, false, 'nothing left to undo');

		for (let i = 0; i < 4; i++) {
			now += 1000;
			const r = command(state, now, { type: 'redo' });
			assert.equal(r.result.ok, true, r.result.error);
			state = JSON.parse(JSON.stringify(r.state));
		}
		assert.equal(count(state), full);
		snap = snapshot(state, now);
		assert.deepEqual([snap.undoSteps, snap.redoSteps], [4, 0]);

		// a new action after an undo drops the redo branch
		now += 1000;
		state = command(state, now, { type: 'undo' }).state;
		now += 1000;
		const fresh = command(state, now, { type: 'paint', cells: [{ x: 1, y: 1 }], color: 'green' });
		assert.equal(fresh.snapshot.redoSteps, 0);
	});

	it('undo is refused while a truck loads at the dock it would remove (core guard): a plain rejection, history untouched', async () => {
		const { state, now } = await buildTutorialLine(newFactoryState(), T0);
		const dock = (state as S).sim.layout.buildings.find((b: S) => b.kind === 'dock');
		const acc = command(state, now + 1000, { type: 'acceptOrder', orderId: (state as S).sim.orders[0].id, dockId: dock.id });
		assert.equal(acc.result.ok, true, acc.result.error);
		const r = command(acc.state, now + 2000, { type: 'undo' });
		assert.deepEqual(r.result, { ok: false, error: 'У рампы уже грузится заказ', code: 'rejected' });
		assert.deepEqual((r.state as S).history, (acc.state as S).history);
	});

	it('the history is capped at UNDO_STEPS and the state stays far below 150 KB', () => {
		let state = newFactoryState();
		let now = T0;
		for (let i = 0; i < UNDO_STEPS + 15; i++) {
			now += 1000;
			const r = command(state, now, { type: 'paint', cells: [{ x: i % 20, y: 2 }], color: i % 2 ? 'red' : 'green' });
			assert.equal(r.result.ok, true, r.result.error);
			state = JSON.parse(JSON.stringify(r.state));
		}
		assert.equal(snapshot(state, now).undoSteps, UNDO_STEPS);
		assert.ok(JSON.stringify(state).length < 20 * 1024);
	});

	it('a big demo factory with a full history stays within the 150 KB audit (core claims ~10 KB)', async () => {
		let { state, now } = await buildTutorialLine(newFactoryState(), T0);
		for (let i = 0; i < UNDO_STEPS; i++) {
			now += 1000;
			const r = command(state, now, { type: 'paint', cells: Array.from({ length: 40 }, (_, k) => ({ x: k % 24, y: (i + k) % 14 })), color: 'yellow' });
			assert.equal(r.result.ok, true, r.result.error);
			state = r.state;
		}
		const bytes = JSON.stringify(state).length;
		assert.ok(bytes < 150 * 1024, `state is ${bytes} bytes`);
		assert.equal(snapshot(state, now).undoSteps, UNDO_STEPS);
	});
});

describe('etaAt: the server schedule of loading orders', () => {
	async function withBigOrder() {
		let { state, now } = await buildTutorialLine(newFactoryState(), T0);
		const dock = (state as S).sim.layout.buildings.find((b: S) => b.kind === 'dock');
		let r = command(state, now + 1000, { type: 'acceptOrder', orderId: (state as S).sim.orders[0].id, dockId: dock.id });
		assert.equal(r.result.ok, true, r.result.error);
		assert.equal(typeof r.snapshot.orders.find((o) => o.status === 'loading')!.etaAt, 'number', 'tutorial order has etaAt');
		now += 3 * H;
		state = catchUp(r.state, now).state;
		state = command(state, now + 1000, { type: 'dialogDone', id: 'q_dock' }).state;
		now += 2000;
		const big = [...(state as S).sim.orders.filter((o: S) => o.status === 'available')].sort((a: S, b: S) => b.qty - a.qty)[0];
		r = command(state, now, { type: 'acceptOrder', orderId: big.id, dockId: dock.id });
		assert.equal(r.result.ok, true, r.result.error);
		return { r, now, id: big.id as string };
	}
	const eta = (snap: GameSnapshot, id: string) => snap.orders.find((o) => o.id === id)!.etaAt;

	it('every loading order has etaAt (a producing line) and it is in the future', async () => {
		const { r, now, id } = await withBigOrder();
		const loading = r.snapshot.orders.filter((o) => o.status === 'loading');
		assert.ok(loading.length >= 1);
		for (const o of loading) {
			assert.equal(typeof o.etaAt, 'number', o.id);
			assert.ok(o.etaAt! >= now);
		}
		assert.ok(eta(r.snapshot, id)! > now + 10 * 60_000, 'a big order needs time');
		for (const o of r.snapshot.orders.filter((q) => q.status !== 'loading')) assert.equal(o.etaAt, undefined, 'only loading orders have etaAt');
	});

	it('a rebuild shifts etaAt: an upgrade pulls it in, cutting the line removes it, undo brings it back', async () => {
		const { r, now, id } = await withBigOrder();
		const base = eta(r.snapshot, id)!;
		const layout = (r.state as S).sim.layout;

		const labeler = layout.buildings.find((b: S) => b.kind === 'labeler');
		const up = command(r.state, now + 1000, { type: 'upgrade', buildingId: labeler.id });
		assert.equal(up.result.ok, true, up.result.error);
		const faster = eta(up.snapshot, id)!;
		assert.ok(faster < base, `upgrade: ${faster} < ${base}`);

		const belt = layout.conveyors.find((c: S) => c.kind === 'belt');
		const cut = command(r.state, now + 1000, { type: 'remove', cells: [{ x: belt.x, y: belt.y }], kind: 'belt' });
		assert.equal(cut.result.ok, true, cut.result.error);
		assert.equal(eta(cut.snapshot, id), undefined, 'the line does not finish the order any more');

		const back = command(JSON.parse(JSON.stringify(cut.state)), now + 2000, { type: 'undo' });
		assert.equal(back.result.ok, true, back.result.error);
		assert.ok(Math.abs(eta(back.snapshot, id)! - base) < 5 * 60_000, 'the schedule is restored');
	});

	it('the schedule is recomputed on catch-up and agrees with reality: the order closes at etaAt', async () => {
		const { r, id } = await withBigOrder();
		const planned = eta(r.snapshot, id)!;
		const before = catchUp(r.state, planned - 60_000).snapshot.orders.find((o) => o.id === id)!;
		assert.equal(before.status, 'loading');
		const after = catchUp(r.state, planned + 60_000).snapshot.orders.find((o) => o.id === id)!;
		assert.equal(after.status, 'done');
	});
});

// helper local to this file
function newFactoryState(): StateObject {
	return bootstrap(null, T0, { id: 'u1', tgId: 1001, name: 'Ann', username: 'ann' }).state;
}
