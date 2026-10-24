// Version conflicts, resync and lost responses through the REAL HttpGameApi against the live stack (http-contract.md "Ретраи и ресинк").

import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api/types';
import { HttpGameApi } from '$lib/api/http/HttpGameApi';
import { START_CREDITS } from '$lib/content/balance';
import { newDevUser, ok, placeBlow, placeIntake, spyHttp, tab } from './helpers';

afterEach(() => vi.restoreAllMocks());

describe('409 and resync', () => {
	it('stale version -> 409 -> GET factory/me -> the command is re-sent once with a new key and passes', async () => {
		const user = newDevUser();
		const a = tab(user), b = tab(user);
		await a.bootstrap();
		await b.bootstrap();
		ok(await b.command(placeBlow)); // tab B moves the factory to version 1; tab A still thinks 0
		const log = spyHttp();
		const r = ok(await a.command(placeIntake));
		expect(log).toEqual(['POST /api/command 409', 'GET /api/factory/me 200', 'POST /api/command 200']);
		expect(r.version).toBe(2);
		expect(r.snapshot.factory.layout.buildings.map((x) => x.kind).sort()).toEqual(['blow', 'intake']);
		expect(r.replayed).toBeFalsy();
	});

	it('undo/redo are never re-sent after a 409: version_conflict + the fresh snapshot', async () => {
		for (const type of ['undo', 'redo'] as const) {
			const user = newDevUser();
			const a = tab(user), b = tab(user);
			await a.bootstrap();
			await b.bootstrap();
			ok(await b.command(placeBlow));
			const log = spyHttp();
			const r = await a.command({ type });
			expect(r).toMatchObject({ ok: false, code: 'version_conflict' });
			if (r.ok) return;
			expect(r.error).toBe('Цех обновился, повтори действие');
			expect(r.snapshot?.factory.layout.buildings).toHaveLength(1);
			expect(r.snapshot?.version).toBe(1);
			expect(log).toEqual(['POST /api/command 409', 'GET /api/factory/me 200']);
			vi.restoreAllMocks();
			expect((await b.sync()).factory.layout.buildings).toHaveLength(1); // nothing was undone behind B's back
		}
	});

	it('advance and sync ignore versions (a stale tab gets no 409); buildEnd is a no-op', async () => {
		const user = newDevUser();
		const a = tab(user), b = tab(user);
		await a.bootstrap();
		await b.bootstrap();
		ok(await b.command(placeBlow));
		const log = spyHttp();
		const adv = await a.advance('heartbeat');
		expect(adv.version).toBe(1);
		expect(log).toEqual(['POST /api/advance 200']);
		vi.restoreAllMocks();
		ok(await b.command(placeIntake));
		const end = ok(await a.command({ type: 'buildEnd' })); // A is stale again; buildEnd must not 409 and must not bump
		expect(end.version).toBe(2);
		expect((await b.sync()).version).toBe(2);
	});

	it('a rejected command keeps the version and carries a fresh snapshot', async () => {
		const a = tab(newDevUser());
		await a.bootstrap();
		const r = await a.command({ type: 'place', kind: 'blow', x: 99, y: 99, rot: 0 });
		expect(r.ok).toBe(false);
		if (r.ok) return;
		expect(r.error.length).toBeGreaterThan(0);
		expect(r.snapshot?.factory.credits).toBe(START_CREDITS);
		expect((await a.sync()).version).toBe(0);
	});
});

/** The first POST /api/command is executed by the server, but its answer is lost (the client sees a network error). */
function loseFirstCommandResponse() {
	const sent: { key: string; version: number }[] = [];
	const orig = globalThis.fetch;
	vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
		const isCommand = init?.method === 'POST' && new URL(String(input)).pathname === '/api/command';
		if (isCommand) {
			const body = JSON.parse(String(init!.body));
			sent.push({ key: body.idempotency_key, version: body.version });
		}
		const res = await orig(input, init);
		if (isCommand && sent.length === 1) throw new TypeError('fetch failed (response lost)');
		return res;
	});
	return sent;
}

describe('lost response', () => {
	it('re-sent with the same key and version: replayed, applied once', async () => {
		const user = newDevUser();
		const a = tab(user);
		await a.bootstrap();
		const sent = loseFirstCommandResponse();
		const r = ok(await a.command(placeBlow));
		expect(sent).toHaveLength(2);
		expect(sent[1]).toEqual(sent[0]); // same idempotency_key, same version
		expect(r.replayed).toBe(true);
		expect(r.version).toBe(1);
		vi.restoreAllMocks();
		const cur = await tab(user).sync();
		expect(cur.factory.layout.buildings.map((x) => x.kind)).toEqual(['blow']); // not twice
		expect(cur.factory.credits).toBe(r.snapshot.factory.credits);
		expect(cur.version).toBe(1);
	});

	it('another tab moved the factory during the pause: the stored answer by key still wins over the version check', async () => {
		const user = newDevUser();
		const other = tab(user);
		await other.bootstrap();
		// the pause before the re-send is the client's own sleep(): the other tab acts exactly then
		const a = tab(user, undefined, async () => {
			vi.restoreAllMocks();
			ok(await other.command(placeIntake));
		});
		await a.bootstrap();
		const sent = loseFirstCommandResponse();
		const r = ok(await a.command(placeBlow));
		expect(sent[0].version).toBe(0);
		expect(r.replayed, 'replayed answer, not 409').toBe(true);
		const cur = await tab(user).sync();
		expect(cur.factory.layout.buildings.map((x) => x.kind).sort()).toEqual(['blow', 'intake']);
		expect(cur.version).toBe(2);
	});
});

describe('no server', () => {
	it('unreachable backend: ApiError network for reads, {ok:false, code:network} for commands', async () => {
		const dead = new HttpGameApi({ baseUrl: 'https://127.0.0.1:1', initData: () => '', devUser: () => newDevUser(), startParam: () => undefined, sleep: async () => {} });
		await expect(dead.bootstrap()).rejects.toSatisfy((e) => e instanceof ApiError && e.code === 'network');
		expect(await dead.command({ type: 'buildEnd' })).toMatchObject({ ok: false, code: 'network' });
	});
});
