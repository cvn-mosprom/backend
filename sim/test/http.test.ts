import assert from 'node:assert/strict';
import type { Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, describe, it } from 'node:test';
import { createSimServer } from '../src/app';
import { BODY_LIMIT_BYTES } from '../src/http';
import { EXTRAS, H, PLAYER, T0, buildTutorialLine, newFactory, pickExtras, shiftCmd, tutorialCommands } from './helpers';

type J = Record<string, any>;

let server: Server;
let base: string;
const errors: Record<string, unknown>[] = [];

before(async () => {
	server = createSimServer({ simCommit: 'abc1234', log: (l) => l.level === 'error' && errors.push(l) });
	await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
	base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => new Promise<void>((resolve) => (server.closeAllConnections(), server.close(() => resolve()))));

async function post(path: string, body: unknown, raw = false): Promise<{ status: number; json: J; headers: Headers }> {
	const res = await fetch(base + path, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: raw ? (body as string) : JSON.stringify(body)
	});
	return { status: res.status, json: (await res.json()) as J, headers: res.headers };
}

const bootstrapBody = (state: unknown = null, now = T0) => ({ state, now, player: PLAYER });

describe('GET /health', () => {
	it('reports ok, service, the sim commit and whether the undo history lives in the state', async () => {
		const res = await fetch(base + '/health');
		assert.equal(res.status, 200);
		// historyInState: undo/redo live in state.history (features.undo of the API follows it)
		assert.deepEqual(await res.json(), { status: 'ok', service: 'sim', simCommit: 'abc1234', historyInState: true, stateV: 2 });
		assert.ok(res.headers.get('x-request-id'));
	});
	it('echoes X-Request-ID', async () => {
		const res = await fetch(base + '/health', { headers: { 'x-request-id': 'req-42' } });
		assert.equal(res.headers.get('x-request-id'), 'req-42');
	});
});

describe('POST /v1/bootstrap', () => {
	it('null state creates a factory and a snapshot', async () => {
		const r = await post('/v1/bootstrap', bootstrapBody());
		assert.equal(r.status, 200);
		assert.equal(r.json.state.player.id, 'u1');
		assert.equal(r.json.snapshot.player.id, 'u1');
		assert.ok(r.json.snapshot.factory);
		assert.ok(Array.isArray(r.json.snapshot.orders));
		assert.ok(r.json.snapshot.season);
		assert.equal(r.json.snapshot.serverTime, T0);
	});
	it('existing state is caught up, unknown fields survive the JSON round trip', async () => {
		const first = await post('/v1/bootstrap', bootstrapBody());
		const state = { ...first.json.state, ...structuredClone(EXTRAS) };
		const r = await post('/v1/bootstrap', bootstrapBody(state, T0 + 5 * 60_000));
		assert.equal(r.status, 200);
		assert.deepEqual(pickExtras(r.json.state), EXTRAS);
		assert.equal(r.json.state.lastTick, T0 + 5 * 60_000);
	});
	it('accepts a numeric player id', async () => {
		const r = await post('/v1/bootstrap', { state: null, now: T0, player: { id: 12345, name: 'N' } });
		assert.equal(r.status, 200);
		assert.equal(r.json.state.player.id, '12345');
	});
});

describe('POST /v1/command', () => {
	it('renameFactory succeeds and the state changes', async () => {
		const { json: b } = await post('/v1/bootstrap', bootstrapBody());
		const r = await post('/v1/command', { state: b.state, now: T0 + 1000, cmd: { type: 'renameFactory', name: 'Квас' } });
		assert.equal(r.status, 200);
		assert.equal(r.json.result.ok, true);
		assert.equal(r.json.state.factoryName, 'Квас');
		assert.equal(r.json.snapshot.factory.name, 'Квас');
		assert.ok(!('snapshot' in r.json.result));
	});
	it('a rule rejection is a normal 200 with result.ok=false', async () => {
		const { json: b } = await post('/v1/bootstrap', bootstrapBody());
		const r = await post('/v1/command', { state: b.state, now: T0 + 1000, cmd: { type: 'renameFactory', name: 'x' } });
		assert.equal(r.status, 200);
		assert.equal(r.json.result.ok, false);
		assert.equal(typeof r.json.result.error, 'string');
		assert.equal(r.json.state.factoryName, 'Мой цех');
	});
	it('a rejection carries the original message and a reason code; submitShift limit/anti-cheat codes', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const r = await post('/v1/command', { state: built.state, now: built.now + 1000, cmd: { type: 'renameFactory', name: 'x' } });
		assert.deepEqual(r.json.result, { ok: false, error: 'Название от 2 символов', code: 'rejected' });
		const short = await post('/v1/command', { state: built.state, now: built.now + 1000, cmd: shiftCmd(built.state, { durationMs: 100 }) });
		assert.equal(short.json.result.code, 'minigame_too_short');
		let state = built.state;
		for (let i = 1; i <= 6; i++) {
			const s = await post('/v1/command', { state, now: built.now + i * 1000, cmd: shiftCmd(state) });
			assert.equal(s.json.result.ok, i <= 5, String(i));
			if (i === 6) assert.equal(s.json.result.code, 'daily_limit_exceeded');
			state = s.json.state;
		}
	});
	it('tick is gone from the contract: the sim answers 400 unknown command type', async () => {
		const { json: b } = await post('/v1/bootstrap', bootstrapBody());
		const r = await post('/v1/command', { state: b.state, now: T0 + 1000, cmd: { type: 'tick', from: T0, to: T0 + 500, events: [] } });
		assert.equal(r.status, 400);
		assert.equal(r.json.error.code, 'bad_request');
	});
	it('undo / redo over HTTP: the history is in the state and survives a JSON re-request', async () => {
		const { json: b } = await post('/v1/bootstrap', bootstrapBody());
		const place = await post('/v1/command', { state: b.state, now: T0 + 1000, cmd: tutorialCommands()[0] });
		assert.equal(place.json.result.ok, true);
		assert.equal(place.json.snapshot.undoSteps, 1);
		const undo = await post('/v1/command', { state: place.json.state, now: T0 + 2000, cmd: { type: 'undo' } });
		assert.equal(undo.json.result.ok, true, undo.json.result.error);
		assert.equal(undo.json.snapshot.factory.layout.buildings.length, 0);
		const redo = await post('/v1/command', { state: undo.json.state, now: T0 + 3000, cmd: { type: 'redo' } });
		assert.equal(redo.json.result.ok, true, redo.json.result.error);
		assert.equal(redo.json.snapshot.factory.layout.buildings.length, 1);
	});
	it('meta survives every kind of command outcome', async () => {
		const { json: b } = await post('/v1/bootstrap', bootstrapBody());
		const state = { ...b.state, ...structuredClone(EXTRAS) };
		for (const cmd of [{ type: 'ageConfirm' }, { type: 'renameFactory', name: '?' }, { type: 'undo' }, { type: 'buildEnd' }]) {
			const r = await post('/v1/command', { state, now: T0 + 2000, cmd });
			assert.equal(r.status, 200);
			assert.deepEqual(pickExtras(r.json.state), EXTRAS, JSON.stringify(cmd));
		}
	});
	it('smoke: builds the tutorial line over HTTP (place + placeConveyor)', async () => {
		let { json: b } = await post('/v1/bootstrap', bootstrapBody());
		let state = b.state;
		let now = T0;
		for (const cmd of tutorialCommands()) {
			now += 1000;
			const r = await post('/v1/command', { state, now, cmd });
			assert.equal(r.status, 200);
			assert.equal(r.json.result.ok, true, `${cmd.type}: ${r.json.result.error}`);
			state = r.json.state;
		}
		assert.equal(state.sim.layout.buildings.length, 8);
		// and the whole thing runs: +3 h produces
		const adv = await post('/v1/advance', { state, now: now + 3 * H });
		assert.equal(adv.status, 200);
		assert.ok(adv.json.snapshot.factory.produced > 0);
	});
});

describe('POST /v1/advance', () => {
	it('returns state, snapshot and advancedMs; keeps meta', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const state = { ...built.state, ...structuredClone(EXTRAS) };
		const r = await post('/v1/advance', { state, now: built.now + 3 * H, reason: 'lazy' });
		assert.equal(r.status, 200);
		assert.deepEqual(Object.keys(r.json).sort(), ['advancedMs', 'snapshot', 'state']);
		assert.equal(r.json.advancedMs, 3 * H);
		assert.equal(r.json.state.lastTick, built.now + 3 * H);
		assert.ok(r.json.snapshot.factory.produced > 0);
		assert.deepEqual(pickExtras(r.json.state), EXTRAS);
	});
	it('reason=background keeps the unseen offline report (accumulates); other reasons replace it', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const a = await post('/v1/advance', { state: built.state, now: built.now + 1 * H, reason: 'resume' });
		const bg = await post('/v1/advance', { state: a.json.state, now: built.now + 1.5 * H, reason: 'background' });
		const manual = await post('/v1/advance', { state: a.json.state, now: built.now + 1.5 * H, reason: 'manual' });
		assert.equal(bg.json.snapshot.offline.hours, 1.5);
		assert.equal(manual.json.snapshot.offline.hours, 0.5);
		assert.ok(bg.json.snapshot.offline.producedBottles > manual.json.snapshot.offline.producedBottles);
		assert.deepEqual(bg.json.state.offline, bg.json.snapshot.offline);
	});
	it('capHours is honoured', async () => {
		const built = await buildTutorialLine(await newFactory(), T0);
		const r1 = await post('/v1/advance', { state: built.state, now: built.now + 5 * H, capHours: 1 });
		const r2 = await post('/v1/advance', { state: built.state, now: built.now + 1 * H });
		assert.equal(r1.json.advancedMs, 5 * H);
		assert.equal(r1.json.snapshot.factory.produced, r2.json.snapshot.factory.produced);
		assert.equal(r1.json.snapshot.offline.hours, 1);
	});
});

describe('POST /v1/snapshot', () => {
	it('returns only the snapshot', async () => {
		const { json: b } = await post('/v1/bootstrap', bootstrapBody());
		const r = await post('/v1/snapshot', { state: b.state, now: T0 + H });
		assert.equal(r.status, 200);
		assert.deepEqual(Object.keys(r.json), ['snapshot']);
		assert.equal(r.json.snapshot.serverTime, T0 + H);
		assert.equal(r.json.snapshot.factory.lastTick, T0);
	});
});

describe('isolation over HTTP', () => {
	it('parallel requests for different players do not leak', async () => {
		const out = await Promise.all(
			Array.from({ length: 32 }, async (_, i) => {
				const player = { id: `p${i}`, name: `Player ${i}` };
				const b = await post('/v1/bootstrap', { state: null, now: T0 + i * 1000, player });
				const c = await post('/v1/command', { state: b.json.state, now: T0 + i * 1000 + 500, cmd: { type: 'renameFactory', name: `Z${i}` } });
				return { i, b, c };
			})
		);
		for (const { i, b, c } of out) {
			assert.equal(b.json.snapshot.player.id, `p${i}`);
			assert.equal(c.json.snapshot.player.id, `p${i}`);
			assert.equal(c.json.snapshot.factory.name, `Z${i}`);
			assert.equal(c.json.snapshot.serverTime, T0 + i * 1000 + 500);
		}
	});
});

describe('errors', () => {
	const expect400 = (r: { status: number; json: J }, what: string) => {
		assert.equal(r.status, 400, `${what}: ${JSON.stringify(r.json)}`);
		assert.equal(r.json.error.code, 'bad_request');
		assert.equal(typeof r.json.error.message, 'string');
	};
	let state: J;
	before(async () => {
		state = (await post('/v1/bootstrap', bootstrapBody())).json.state;
	});

	it('malformed JSON -> 400 bad_request', async () => {
		expect400(await post('/v1/bootstrap', '{"state": nul', true), 'truncated json');
		expect400(await post('/v1/command', '', true), 'empty body');
	});
	it('non-object body -> 400', async () => {
		expect400(await post('/v1/snapshot', '[1,2]', true), 'array');
		expect400(await post('/v1/snapshot', '"x"', true), 'string');
	});
	it('missing / invalid fields -> 400', async () => {
		expect400(await post('/v1/bootstrap', { state: null, player: PLAYER }), 'no now');
		expect400(await post('/v1/bootstrap', { state: null, now: 'soon', player: PLAYER }), 'now not a number');
		expect400(await post('/v1/bootstrap', { now: T0, player: PLAYER }), 'state missing (would silently reset a factory)');
		expect400(await post('/v1/bootstrap', { state: null, now: T0 }), 'player missing for null state');
		expect400(await post('/v1/bootstrap', { state: null, now: T0, player: { id: '', name: 'A' } }), 'empty player id');
		expect400(await post('/v1/bootstrap', { state: null, now: T0, player: { id: 'a' } }), 'player name missing');
		expect400(await post('/v1/bootstrap', { state: null, now: T0, player: { id: 'a', name: 'A', tgId: 'x' } }), 'tgId not int');
		expect400(await post('/v1/command', { state, now: T0 }), 'cmd missing');
		expect400(await post('/v1/command', { state: null, now: T0, cmd: { type: 'buildEnd' } }), 'state null for command');
		expect400(await post('/v1/command', { state, now: T0, cmd: { type: 'launchRocket' } }), 'unknown command');
		expect400(await post('/v1/command', { state, now: T0, cmd: { type: 'constructor' } }), 'prototype key as command');
		expect400(await post('/v1/command', { state, now: T0, cmd: 'ageConfirm' }), 'cmd not an object');
		expect400(await post('/v1/advance', { state, now: T0, capHours: -1 }), 'negative cap');
		expect400(await post('/v1/advance', { state, now: T0, capHours: 'x' }), 'cap not a number');
		expect400(await post('/v1/advance', { state, now: T0, reason: 5 }), 'reason not a string');
		expect400(await post('/v1/snapshot', { now: T0 }), 'state missing');
	});
	it('a body that is not a factory state -> 400', async () => {
		expect400(await post('/v1/snapshot', { state: {}, now: T0 }), 'empty state');
		expect400(await post('/v1/snapshot', { state: { ...state, v: 3 }, now: T0 }), 'newer than the core');
		expect400(await post('/v1/snapshot', { state: { ...state, v: 0 }, now: T0 }), 'wrong version');
		expect400(await post('/v1/snapshot', { state: { ...state, sim: null }, now: T0 }), 'no sim');
		expect400(await post('/v1/snapshot', { state: { ...state, sim: { ...state.sim, layout: {} } }, now: T0 }), 'no layout');
		expect400(await post('/v1/snapshot', { state: { ...state, season: undefined }, now: T0 }), 'no season');
	});
	it('unknown path -> 404, wrong method -> 405', async () => {
		const nf = await fetch(base + '/v1/nope', { method: 'POST', body: '{}' });
		assert.equal(nf.status, 404);
		assert.equal(((await nf.json()) as J).error.code, 'not_found');
		const mna = await fetch(base + '/v1/command');
		assert.equal(mna.status, 405);
		const mna2 = await fetch(base + '/health', { method: 'POST', body: '{}' });
		assert.equal(mna2.status, 405);
	});
	it('oversized body -> 413 payload_too_large', async () => {
		const r = await post('/v1/snapshot', JSON.stringify({ state, now: T0, pad: 'x'.repeat(BODY_LIMIT_BYTES + 10) }), true);
		assert.equal(r.status, 413);
		assert.equal(r.json.error.code, 'payload_too_large');
	});
	it('an exception inside the core -> 500 internal_error, and the server keeps serving', async () => {
		errors.length = 0;
		const r = await post('/v1/command', { state, now: T0 + 1000, cmd: { type: 'place' } }); // schema-invalid: no kind
		assert.equal(r.status, 500);
		assert.equal(r.json.error.code, 'internal_error');
		assert.equal(errors.length, 1);
		const ok = await post('/v1/command', { state, now: T0 + 1000, cmd: { type: 'ageConfirm' } });
		assert.equal(ok.status, 200);
		assert.equal(ok.json.result.ok, true);
		assert.equal((await fetch(base + '/health')).status, 200);
	});
});
