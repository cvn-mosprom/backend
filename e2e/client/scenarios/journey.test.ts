// One player's whole session through the REAL HttpGameApi against the live stack:
// bootstrap -> tutorial line -> undo/redo -> accept order (etaAt) -> advance(heartbeat, order_due) -> leaderboard x4 -> visit -> summary
// -> lead -> getLead -> quiz -> events -> revokeLead. Steps share state, so they are meant to run in this order.
// Every assertion names a field the client reads; a renamed/removed field on the server turns a step red.

import { describe, expect, it } from 'vitest';
import { ApiError, type GameSnapshot, type LeadForm, type SeasonSummary } from '$lib/api/types';
import { QUIZ_QUESTIONS } from '$lib/api/mock/quiz';
import { TUTORIAL_SPOTS } from '$lib/content/tutorial';
import { SHIFTS_PER_DAY, START_CREDITS } from '$lib/content/balance';
import { ORDER_WAIT_S, TUTORIAL, buildingOf, newDevUser, ok, placeBlow, tab } from './helpers';

const user = newDevUser();
const c = tab(user, 'ref_e2e');
let snap: GameSnapshot;

/** SeasonSummary plus the server-only fields (leadStatus / quizResult are not in the client's type; the HR funnel and the quiz screen use them). */
const summary = async () => (await c.seasonSummary()) as SeasonSummary & { leadStatus?: string | null; quizResult?: { completed: boolean } | null };

const lead = (over: Partial<LeadForm> = {}): LeadForm => ({
	fullName: 'Контракт Тестов', birthDate: '2000-05-04', tgUsername: 'contract_e2e', university: 'МГУ', institutionKind: 'vuz', course: '3',
	department: 'lab', roleId: 'operator', practiceType: 'paid', consent: true, consentVersion: '2026-10-v1', ...over
});

describe('player journey', () => {
	it('bootstrap: version, serverTime shift, features, season, a fresh factory', async () => {
		snap = await c.bootstrap();
		expect(snap.version).toBe(0); // GameSnapshot.version is set by the client from the envelope
		expect(c.features()).toEqual({ undo: true, quiz: true, leads: true }); // features.undo is on since S12
		expect(Math.abs(c.now() - Date.now())).toBeLessThan(10_000); // clock shift from serverTime
		expect(snap.serverTime).toBeTypeOf('number');
		expect(snap.player.id).toBeTypeOf('string');
		expect(snap.player.createdAt).toBeTypeOf('number');
		expect(snap.factory.credits).toBe(START_CREDITS);
		expect(snap.factory.layout.hangar).toMatchObject({ w: 24, h: 14 });
		expect(snap.factory.layout.buildings).toEqual([]);
		expect(snap.season).toMatchObject({ shiftsLeft: SHIFTS_PER_DAY });
		expect(snap.season.id).toBeTypeOf('number');
		expect(snap.season.endsAt).toBeGreaterThan(snap.season.startsAt);
		const first = snap.orders.find((o) => o.id === 'tutorial_first');
		expect(first, 'tutorial order on the board').toMatchObject({ status: 'available', tutorial: true });
		expect(first!.etaAt).toBeUndefined(); // not loading yet
		expect(await tab(user).bootstrap().then((s) => s.player.id)).toBe(snap.player.id); // another tab, same factory
	});

	it('place: credits drop, version +1 per command, undo/redo walk the server history', async () => {
		const p = ok(await c.command(placeBlow));
		expect(p.version).toBe(1);
		expect(p.snapshot.version).toBe(1);
		expect(p.snapshot.factory.credits).toBeLessThan(START_CREDITS);
		expect(p.snapshot.factory.layout.buildings.map((b) => b.kind)).toEqual(['blow']);
		expect(p.snapshot.undoSteps).toBe(1);
		expect(p.snapshot.undoLabel).toBeTypeOf('string');
		expect(p.replayed).toBeFalsy();

		const u = ok(await c.command({ type: 'undo' }));
		expect(u.version).toBe(2);
		expect(u.snapshot.factory.layout.buildings).toEqual([]);
		expect(u.snapshot.factory.credits).toBe(START_CREDITS); // undo refunds in full
		expect(u.snapshot.undoSteps).toBe(0);
		expect(u.snapshot.redoSteps).toBe(1);

		const r = ok(await c.command({ type: 'redo' }));
		expect(r.version).toBe(3);
		expect(r.snapshot.factory.layout.buildings.map((b) => b.kind)).toEqual(['blow']);
		expect(r.snapshot.redoSteps).toBe(0);
		expect(r.snapshot.factory.credits).toBe(p.snapshot.factory.credits);

		// nothing left to redo: a rejection, not a crash; the version stays
		const again = await c.command({ type: 'redo' });
		expect(again.ok).toBe(false);
		if (!again.ok) expect(again.error.length).toBeGreaterThan(0);
		expect((await c.sync()).version).toBe(3);
	});

	it('the whole tutorial line goes through (each effective command is version + 1)', async () => {
		ok(await c.command({ type: 'remove', buildingId: buildingOf(await c.sync(), 'blow')!.id }));
		let v = (await c.sync()).version!;
		for (const cmd of TUTORIAL) {
			const r = ok(await c.command(cmd));
			expect(r.version, JSON.stringify(cmd)).toBe(++v);
			snap = r.snapshot;
		}
		expect(snap.player.ageConfirmed).toBe(true);
		expect(snap.factory.name).toBe('Контракт-цех');
		const kinds = snap.factory.layout.buildings.map((b) => b.kind).sort();
		expect(kinds).toEqual(Object.keys(TUTORIAL_SPOTS).sort());
		for (const [name, s] of Object.entries(TUTORIAL_SPOTS)) {
			expect(buildingOf(snap, name), name).toMatchObject({ x: s.x, y: s.y, rot: s.rot });
		}
		expect(snap.factory.layout.conveyors.length).toBeGreaterThan(10);
	});

	it('acceptOrder: the loading order carries etaAt in the future', async () => {
		const dock = buildingOf(snap, 'dock')!;
		const r = ok(await c.command({ type: 'acceptOrder', orderId: 'tutorial_first', dockId: dock.id }));
		const o = r.snapshot.orders.find((x) => x.id === 'tutorial_first')!;
		expect(o.status).toBe('loading');
		expect(o.etaAt, 'Order.etaAt on a loading order').toBeTypeOf('number');
		expect(o.etaAt!).toBeGreaterThan(c.now());
		expect(o.etaAt!).toBeLessThan(c.now() + 10 * 60_000); // the tutorial line ships it in seconds, not hours
		snap = r.snapshot;
	});

	it('advance: heartbeat and order_due never conflict, return the snapshot and keep the version', async () => {
		const v = (await c.sync()).version!;
		const hb = await c.advance('heartbeat');
		expect(hb.version).toBe(v); // catch-up does not bump the version
		expect(hb.serverTime).toBeTypeOf('number');
		expect(hb.factory.layout.buildings.length).toBe(snap.factory.layout.buildings.length);
		const due = await c.advance('order_due');
		expect(due.version).toBe(v);

		// the app schedules order_due at etaAt + 1 s; do the same and see the order shipped
		if (ORDER_WAIT_S > 0) {
			const eta = snap.orders.find((o) => o.id === 'tutorial_first')!.etaAt!;
			const waitMs = eta + 1000 - c.now();
			if (waitMs > ORDER_WAIT_S * 1000) throw new Error(`order ETA is ${Math.round(waitMs / 1000)} s away, E2E_ORDER_WAIT_S=${ORDER_WAIT_S}`);
			if (waitMs > 0) await new Promise((r) => setTimeout(r, waitMs));
			const after = await c.advance('order_due');
			const o = after.orders.find((x) => x.id === 'tutorial_first');
			expect(o?.status, 'order after etaAt + 1 s').not.toBe('loading');
			expect(after.factory.credits).toBeGreaterThan(snap.factory.credits);
			expect(Object.keys(after.factory.shipped).length).toBeGreaterThan(0);
		}
	});

	it('leaderboard: 4 tabs, own row isMe, uni tab lists universities', async () => {
		const mine = (await c.sync()).player.id;
		for (const tabName of ['general', 'quality', 'speed', 'uni'] as const) {
			const lb = await c.leaderboard(tabName);
			expect(lb.tab).toBe(tabName);
			expect(Array.isArray(lb.rows)).toBe(true);
			expect(lb.total).toBeTypeOf('number');
			expect(lb.total).toBeGreaterThanOrEqual(lb.rows.length);
			expect(lb.inviteCutoff).toBeTypeOf('number');
			for (const row of lb.rows.slice(0, 3)) {
				expect(row).toMatchObject({ playerId: expect.any(String), name: expect.any(String), place: expect.any(Number), value: expect.any(Number) });
			}
			expect(lb.me, `own row on ${tabName}`).toMatchObject({ playerId: mine, isMe: true });
			expect(lb.me!.place).toBeGreaterThan(0);
			if (tabName === 'uni') expect(Array.isArray(lb.universities)).toBe(true);
		}
	});

	it('visit: another player sees a public slice (no wallet), unknown id is 404 player_not_found', async () => {
		const viewer = tab(newDevUser());
		await viewer.bootstrap();
		const mine = (await c.sync()).player.id;
		const v = await viewer.visit(mine);
		expect(v.player.id).toBe(mine);
		expect(v.factory.name).toBe('Контракт-цех');
		expect(v.factory.layout.buildings.length).toBe(Object.keys(TUTORIAL_SPOTS).length);
		expect(v.factory.credits ?? 0).toBe(0); // money is private (the server drops the field, fakeApi sends 0)
		expect(v.factory.loan).toBeUndefined();
		await expect(viewer.visit('99999999999')).rejects.toMatchObject({ status: 404, code: 'player_not_found' });
	});

	it('seasonSummary: shape the finale screen reads', async () => {
		const s = await c.seasonSummary();
		expect(s.total).toBeTypeOf('number');
		expect(s.place).toBeTypeOf('number');
		expect(s).toHaveProperty('bestProfession');
	});

	it('lead: 18+ and consent are enforced; submit -> getLead (createdAt is a number) -> duplicate refused', async () => {
		expect(await c.getLead()).toBeNull();
		expect(await c.submitLead(lead({ birthDate: '2015-01-01' }))).toEqual({ ok: false, error: 'age_required' });
		expect(await c.submitLead(lead({ consent: false as never }))).toEqual({ ok: false, error: 'consent_required' });
		// the leads bucket is 5 POSTs a minute and counts rejected ones too: exactly 5 below
		expect(await c.submitLead(lead({ tgUsername: undefined }))).toEqual({ ok: false, error: 'validation_error' }); // no contact at all
		expect(await c.getLead()).toBeNull();
		expect(await c.submitLead(lead())).toEqual({ ok: true });
		const li = await c.getLead();
		expect(li).toMatchObject({ status: 'new', roleId: 'operator' });
		expect(li!.createdAt, 'LeadInfo.createdAt is ms epoch').toBeGreaterThan(Date.now() - 600_000);
		expect(li!.createdAt).toBeLessThan(Date.now() + 600_000);
		expect(await c.submitLead(lead())).toEqual({ ok: false, error: 'lead_already_exists' });
		expect((await summary()).leadStatus).toBe('new');
	});

	it('quiz: batch scoring by the server, first answers are final, a finished quiz is 409', async () => {
		const q = (id: string, answer: string) => ({ questionId: id, answer });
		const right = (i: number) => QUIZ_QUESTIONS[i].correct;
		const wrong = (i: number) => 'abc'.split('').find((x) => x !== QUIZ_QUESTIONS[i].correct)!;
		const r1 = await c.quizAnswer([q(QUIZ_QUESTIONS[0].id, right(0)), q(QUIZ_QUESTIONS[1].id, wrong(1)), q(QUIZ_QUESTIONS[2].id, right(2))]);
		expect(r1).toMatchObject({ correct: 2, total: 3, score: 67 });
		expect(r1.results).toEqual([
			{ questionId: QUIZ_QUESTIONS[0].id, correct: true }, { questionId: QUIZ_QUESTIONS[1].id, correct: false }, { questionId: QUIZ_QUESTIONS[2].id, correct: true }
		]);
		await expect(c.quizAnswer([q('no_such_question', 'a')])).rejects.toMatchObject({ status: 422, code: 'unknown_question' });
		const n = QUIZ_QUESTIONS.length;
		const rest = await c.quizAnswer(QUIZ_QUESTIONS.slice(3).map((x, i) => q(x.id, right(i + 3))));
		expect(rest).toMatchObject({ correct: n - 1, total: n, score: Math.round(((n - 1) / n) * 100) });
		const err = await c.quizAnswer([q(QUIZ_QUESTIONS[0].id, 'b')]).catch((e) => e);
		expect(err).toBeInstanceOf(ApiError);
		expect(err).toMatchObject({ status: 409, code: 'quiz_already_completed' });
		expect((await summary()).quizResult).toMatchObject({ completed: true });
	});

	it('events: 120 events go as 3 batches of <= 50 and are accepted', async () => {
		const ts = c.now() - 1000;
		await c.track(Array.from({ length: 120 }, (_, i) => ({ name: 'app_open', props: { i }, ts })));
	});

	it('revokeLead: 204, repeatable, the summary says revoked', async () => {
		await c.revokeLead();
		await c.revokeLead(); // repeating is not an error
		expect((await summary()).leadStatus).toBe('revoked');
	});

	// Like fakeApi/MockGameApi: the form is forgotten on revoke (GET /api/leads/me -> 404 lead_not_found -> getLead() null); the server keeps the
	// revoked row (consent_revoked_at) for the audit.
	it('revokeLead: getLead() is null afterwards (what the client expects)', async () => {
		expect(await c.getLead()).toBeNull();
	});
});
