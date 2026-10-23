// /v1/advance reason=background must not overwrite an unseen offline report with a smaller one.

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { OFFLINE_CAP_H } from '$lib/content/balance';
import { advance, catchUp, command, mergeOffline } from '../src/engine';
import type { StateObject } from '../src/host';
import { H, T0, buildTutorialLine, newFactory } from './helpers';

type Report = { hours: number; capped: boolean; producedBottles: number; producedBoxes: Record<string, number>; creditsSpent: number; broken: string[]; storageFull: boolean };
const rep = (s: StateObject) => (s as { offline?: Report }).offline;

let built: { state: StateObject; now: number };
before(async () => {
	built = await buildTutorialLine(await newFactory(), T0);
});

/** State after a first absence of `h1` hours: it holds the unseen report A. */
async function withReportA(h1: number) {
	const a = await advance(built.state, built.now + h1 * H, { reason: 'resume' });
	assert.ok(rep(a.state), 'first absence leaves a report');
	return a;
}

describe('advance reason=background accumulates the unseen offline report', () => {
	it('1 h away, then a 30 min background step: one report for 1.5 h (sums, same totals as the parts)', async () => {
		const a = await withReportA(1);
		const A = rep(a.state)!;
		const t2 = built.now + 1.5 * H; // the tutorial storage fills up after ~1.7 h: stay below it so both parts produce
		// the report of the second step alone (for the expected sums)
		const alone = await catchUp(Object.assign({}, a.state, { offline: undefined }), t2);
		const B = rep(alone.state)!;
		assert.equal(A.hours, 1);
		assert.equal(B.hours, 0.5);
		assert.ok(A.producedBottles > 0 && B.producedBottles > 0, 'both parts produced something');

		const merged = await advance(a.state, t2, { reason: 'background' });
		const M = rep(merged.state)!;
		assert.equal(M.hours, 1.5);
		assert.equal(M.capped, false);
		assert.equal(M.producedBottles, A.producedBottles + B.producedBottles);
		assert.ok(M.producedBottles > A.producedBottles, 'the whole absence is visible, not just the last step');
		assert.equal(M.creditsSpent, A.creditsSpent + B.creditsSpent);
		for (const sku of new Set([...Object.keys(A.producedBoxes), ...Object.keys(B.producedBoxes)])) {
			assert.equal(M.producedBoxes[sku] ?? 0, (A.producedBoxes[sku] ?? 0) + (B.producedBoxes[sku] ?? 0), sku);
		}
		assert.deepEqual(merged.snapshot.offline, M, 'the snapshot shows the merged report');
		// the factory itself advanced exactly as without the policy
		assert.deepEqual({ ...merged.state, offline: undefined }, { ...alone.state, offline: undefined });
		assert.equal(merged.advancedMs, 0.5 * H);
	});

	it('the other reasons (and none) keep the mock behaviour: the new report replaces the old one', async () => {
		const a = await withReportA(3);
		for (const reason of ['manual', 'resume', 'whatever', undefined]) {
			const r = await advance(a.state, built.now + 5 * H, { reason });
			assert.equal(rep(r.state)!.hours, 2, String(reason));
		}
	});

	it('hours are capped at OFFLINE_CAP_H and "capped" is set', async () => {
		const a = await withReportA(6);
		const r = await advance(a.state, built.now + 11 * H, { reason: 'background' });
		const M = rep(r.state)!;
		assert.equal(M.hours, OFFLINE_CAP_H);
		assert.equal(M.capped, true);
		assert.equal(r.snapshot.offline?.hours, OFFLINE_CAP_H);
		// a capped part stays capped even when the sum is small
		const small = mergeOffline({ ...rep(a.state)!, hours: 1, capped: true }, { ...rep(a.state)!, hours: 1, capped: false });
		assert.deepEqual([small.hours, small.capped], [2, true]);
	});

	it('a step too short for a report (< 10 min) leaves the unseen report untouched', async () => {
		const a = await withReportA(3);
		const A = structuredClone(rep(a.state));
		const r = await advance(a.state, built.now + 3 * H + 5 * 60_000, { reason: 'background' });
		assert.deepEqual(rep(r.state), A);
		assert.deepEqual(r.snapshot.offline, A);
		assert.equal(r.advancedMs, 5 * 60_000, 'time still advanced');
	});

	it('without an unseen report background is a plain catch-up', async () => {
		const plain = await catchUp(built.state, built.now + 3 * H);
		const bg = await advance(built.state, built.now + 3 * H, { reason: 'background' });
		assert.deepEqual(bg, plain);
	});

	it('after offlineSeen the next background report starts from scratch', async () => {
		const a = await withReportA(3);
		const seen = await command(a.state, built.now + 3 * H + 1000, { type: 'offlineSeen' });
		assert.equal(rep(seen.state), undefined);
		const r = await advance(seen.state, built.now + 5 * H, { reason: 'background' });
		assert.ok(rep(r.state)!.hours < 2 && rep(r.state)!.hours > 1.99, 'only the time after offlineSeen (not 3 + 2 h)');
	});

	it('two background steps in a row, then the player returns: one report for the whole absence', async () => {
		const a = await withReportA(2);
		const b = await advance(a.state, built.now + 4 * H, { reason: 'background' });
		const c = await advance(b.state, built.now + 6 * H, { reason: 'background' });
		assert.equal(rep(c.state)!.hours, 6);
		assert.ok(rep(c.state)!.producedBottles >= rep(b.state)!.producedBottles);
		// the player opens the game: resume replaces nothing it should not (a resume right away has no new report)
		const resume = await advance(c.state, built.now + 6 * H + 1000, { reason: 'resume' });
		assert.deepEqual(rep(resume.state), rep(c.state));
	});

	it('the capHours option still applies to the step', async () => {
		const a = await withReportA(3);
		const r = await advance(a.state, built.now + 9 * H, { reason: 'background', capHours: 1 });
		assert.equal(r.advancedMs, 6 * H);
		assert.equal(rep(r.state)!.hours, 4, '3 h + 1 h of the capped step');
	});
});

describe('mergeOffline', () => {
	const base: Report = { hours: 1, capped: false, producedBottles: 10, producedBoxes: { kvas_pet15: 2 }, creditsSpent: 5, broken: ['Розлив'], storageFull: false };
	it('sums, merges unique broken, ORs storageFull, does not mutate its inputs', () => {
		const a = structuredClone(base);
		const b: Report = { hours: 2.5, capped: false, producedBottles: 7, producedBoxes: { kvas_pet15: 1, kvas_glass: 4 }, creditsSpent: 3, broken: ['Розлив', 'Укупорка'], storageFull: true };
		const bCopy = structuredClone(b);
		const m = mergeOffline(a, b);
		assert.deepEqual(m, { hours: 3.5, capped: false, producedBottles: 17, producedBoxes: { kvas_pet15: 3, kvas_glass: 4 }, creditsSpent: 8, broken: ['Розлив', 'Укупорка'], storageFull: true });
		assert.deepEqual(a, base);
		assert.deepEqual(b, bCopy);
	});
});
