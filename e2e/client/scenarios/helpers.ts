// Shared helpers of the contract scenarios: the REAL HttpGameApi (vendor/frontend) against the live stack.

import { expect, vi } from 'vitest';
import { HttpGameApi } from '$lib/api/http/HttpGameApi';
import type { Command, GameSnapshot } from '$lib/api/types';
import { TUTORIAL_LINKS, TUTORIAL_ROW, TUTORIAL_SPOTS } from '$lib/content/tutorial';

export const BASE = (process.env.E2E_BASE ?? 'https://localhost').replace(/\/+$/, '');
/** Seconds to wait for the tutorial shipment (the tutorial line needs ~55 s); 0 = stop right after "loading started". */
export const ORDER_WAIT_S = Number(process.env.E2E_ORDER_WAIT_S ?? '90');

/**
 * A fresh dev player id: the server takes any positive 64-bit integer, and ids >= 10^12 are the reserved test range (api/app/testusers.py:
 * X-Dev-User carries no name here, so this is what flags the player is_test and keeps it out of the top / HR stats).
 * 10^12 .. 10^12 + 9*10^12 stays an exact JS integer (< 2^53) and below 2^63 - 1.
 */
export const newDevUser = () => String(1_000_000_000_000 + Math.floor(Math.random() * 9_000_000_000_000));

/**
 * One browser tab of `user`, configured like the app: baseUrl from VITE_API_URL (vitest.config.ts), no Telegram initData -> X-Dev-User.
 * The retry pause (the app waits 1 s before re-sending after a dropped connection) is not waited for, or is a hook.
 */
export const tab = (user: string, startParam?: string, sleep: (ms: number) => Promise<void> = async () => {}) =>
	new HttpGameApi({ initData: () => '', startParam: () => startParam, devUser: () => user, sleep });

/** Log of the client's HTTP calls: "POST /api/command 409". */
export function spyHttp() {
	const log: string[] = [];
	const orig = globalThis.fetch;
	vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
		const r = await orig(input, init);
		log.push(`${init?.method ?? 'GET'} ${new URL(String(input)).pathname} ${r.status}`);
		return r;
	});
	return log;
}

export function ok<T extends { ok: boolean }>(r: T): Extract<T, { ok: true }> {
	expect(r.ok, JSON.stringify(r)).toBe(true);
	return r as Extract<T, { ok: true }>;
}

// ---- the tutorial line (same positions as the client's tutorialLayout) ----
const S = TUTORIAL_SPOTS;
const place = (k: keyof typeof S): Command => ({ type: 'place', kind: S[k].kind, x: S[k].x, y: S[k].y, rot: S[k].rot });
const belt = (xs: readonly number[]): Command => ({
	type: 'placeConveyor', kind: 'belt', path: xs.map((x) => ({ x, y: TUTORIAL_ROW, dir: 'E' as const }))
});
const L = TUTORIAL_LINKS;

export const placeBlow = place('blow');
export const placeIntake = place('intake');
export const beltIntakeBlow = belt(L.intake_blow);

/** Intro + the whole tutorial line, in the order the quests ask for it. */
export const TUTORIAL: Command[] = [
	{ type: 'ageConfirm' },
	{ type: 'renameFactory', name: 'Контракт-цех' },
	{ type: 'dialogDone', id: 'q_intro' },
	place('brew_tank'),
	{ type: 'dialogDone', id: 'q_brew' },
	place('intake'), place('blow'), belt(L.intake_blow),
	{ type: 'dialogDone', id: 'q_bottles' },
	place('filler'), belt(L.blow_filler),
	{ type: 'placeConveyor', kind: 'pipe', path: [{ x: S.filler.x, y: TUTORIAL_ROW - 2, dir: 'S' }] },
	{ type: 'dialogDone', id: 'q_filler' },
	place('capper'), belt(L.filler_capper),
	place('labeler'), belt(L.capper_labeler),
	place('packer'), belt(L.labeler_packer),
	{ type: 'dialogDone', id: 'q_line' },
	place('dock'), belt(L.packer_dock)
];

export const buildingOf = (s: GameSnapshot, kind: string) => s.factory.layout.buildings.find((b) => b.kind === kind);
