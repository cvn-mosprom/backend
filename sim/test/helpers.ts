import assert from 'node:assert/strict';
import type { Command } from '$lib/api/types';
import { TUTORIAL_LINKS, TUTORIAL_PIPE, TUTORIAL_ROW, TUTORIAL_SPOTS } from '$lib/content/tutorial';
import { bootstrap, command } from '../src/engine';
import type { StateObject } from '../src/host';

export const H = 3600_000;
/** 2026-10-24 12:00 MSK. */
export const T0 = Date.parse('2026-10-24T09:00:00Z');
export const PLAYER = { id: 'u1', tgId: 1001, name: 'Ann', username: 'ann' };

/** Unknown top-level fields the API owns; the sim must hand them back untouched. */
export const EXTRAS = {
	meta: { idempotency: [{ key: 'k1', at: 1, result: { ok: true, toast: 'hi' } }, { key: 'k2', at: 2 }], rev: 7 },
	futureTop: { a: [1, { b: null, c: 'é' }], d: 0.5 }
};

export async function newFactory(now = T0, player = PLAYER): Promise<StateObject> {
	return (await bootstrap(null, now, player)).state;
}

/** Commands that build the tutorial kvas line exactly as the quests guide the player (content/tutorial.ts). */
export function tutorialCommands(): Command[] {
	const S = TUTORIAL_SPOTS;
	const L = TUTORIAL_LINKS;
	const y = TUTORIAL_ROW;
	const cmds: Command[] = [];
	const place = (k: keyof typeof S) => cmds.push({ type: 'place', kind: S[k].kind, x: S[k].x, y: S[k].y, rot: S[k].rot });
	const belt = (xs: readonly number[]) => cmds.push({ type: 'placeConveyor', kind: 'belt', path: xs.map((x) => ({ x, y, dir: 'E' as const })) });
	place('brew_tank');
	cmds.push({ type: 'placeConveyor', kind: 'pipe', path: TUTORIAL_PIPE.map((c) => ({ x: c.x, y: c.y, dir: 'S' as const })) });
	place('intake'); belt(L.intake_blow);
	place('blow'); belt(L.blow_filler);
	place('filler'); belt(L.filler_capper);
	place('capper'); belt(L.capper_labeler);
	place('labeler'); belt(L.labeler_packer);
	place('packer'); belt(L.packer_dock);
	place('dock');
	return cmds;
}

/** Build the whole tutorial line through the sim, one command per second starting after `now`. Returns the state and the clock. */
export async function buildTutorialLine(state: StateObject, now: number): Promise<{ state: StateObject; now: number }> {
	for (const c of tutorialCommands()) {
		now += 1000;
		const r = await command(state, now, c);
		assert.equal(r.result.ok, true, `${c.type} ${(c as { kind?: string }).kind ?? ''}: ${r.result.error}`);
		state = r.state;
	}
	return { state, now };
}

export const pickExtras = (s: StateObject) => ({ meta: s.meta, futureTop: s.futureTop });
export const withExtras = (s: StateObject): StateObject => ({ ...s, ...structuredClone(EXTRAS) });
