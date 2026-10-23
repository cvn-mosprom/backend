import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import type { Command } from '$lib/api/types';
import { command } from '../src/engine';
import type { StateObject } from '../src/host';
import { GENERIC_REASON, REJECTION_REASONS, rejectionCode, type RejectionReason } from '../src/reasons';
import { T0, buildTutorialLine, newFactory, season, shiftCmd } from './helpers';

// Every rejection message found in the vendored lib (MockGameApi.ts, sim/build.ts, loan.ts, economy.ts, history.ts,
// content/stoplist.ts and the check* helpers of build.ts), grouped by the reason it must map to. Numbers are samples.
const EXPECTED: Record<RejectionReason, string[]> = {
	daily_limit_exceeded: ['Смены на сегодня кончились — завтра 5 новых'],
	season_closed: ['Сезон закрыт'],
	insufficient_funds: ['Не хватает 120 🪙', 'Нужно 300 🪙, чтобы вернуть снесённое', 'Нет 🪙, чтобы вернуть'],
	not_found: ['Станция не найдена', 'Здание не найдено', 'Нет такого здания', 'Заказ недоступен', 'Нужна рампа отгрузки'],
	// answered by command type in engine.ts (the mock has no message for them)
	tick_removed: [],
	history_unavailable: [],
	// 'Смена не засчитана' depends on the cmd: covered separately below
	minigame_too_short: [],
	impossible_score: [],
	rejected: [
		'Название от 2 символов', 'Без ругательств, пожалуйста', 'Цех безалкогольный — выбери другое название', 'Без чужих брендов — придумай своё название',
		'У рампы стоит грузовик', 'У этой рампы уже стоит грузовик', 'У рампы уже грузится заказ', 'Продукт откроется на ур. 4',
		'Нечего отменять', 'Нечего повторять', 'Нечего продавать', 'Учебный заказ без срока — грузовик дождётся', 'Неизвестная команда',
		'Не та профессия для станции',
		'Откроется на ур. 3', 'Петля в схеме', 'Петля: лента замкнута сама на себя', 'Нельзя', 'Не повернуть: мешают соседи', 'Максимальный уровень',
		'Не улучшается', 'Уже максимум', 'Ремонт не нужен', 'Эта настройка пока закрыта', 'Ангар максимальный', 'Расширяем только вправо или вниз',
		'По ширине максимум', 'По глубине максимум', 'Вне ангара', 'Тут колонна', 'Занято', 'Занято лентой', 'Занято зданием',
		'Путь пересекает сам себя', 'Ставится у ворот, стороной к стене (↻)', 'Порт смотрит в стену (↻)', 'Пустой путь',
		'Сумма должна быть больше нуля', 'Аванс уже выдан — сначала верни его', 'Лимит аванса сейчас — 500 🪙', 'Аванса нет'
	]
};

const SHIFT_MSG = 'Смена не засчитана';
const norm = (m: string) => m.replace(/\$\{[^}]*\}/g, '#').replace(/\d+/g, '#');
const known = new Set([...Object.values(EXPECTED).flat(), SHIFT_MSG].map(norm));

describe('rejectionCode: the Russian message -> reason table', () => {
	it('every known message maps as intended', () => {
		let n = 0;
		for (const [reason, messages] of Object.entries(EXPECTED)) {
			for (const m of messages) {
				assert.equal(rejectionCode(m), reason, m);
				n++;
			}
		}
		assert.ok(n > 50);
	});

	it('submitShift "Смена не засчитана" is split by the cmd: too short vs impossible score', () => {
		const shift = (durationMs: number, score: number) =>
			({ type: 'submitShift', buildingId: 'b1', result: { gameId: 'g', profession: 'operator', result: 'win', score, durationMs, difficulty: 1, metrics: {} } }) as Command;
		assert.equal(rejectionCode(SHIFT_MSG, shift(399, 0.5)), 'minigame_too_short');
		assert.equal(rejectionCode(SHIFT_MSG, shift(0, 0.5)), 'minigame_too_short');
		assert.equal(rejectionCode(SHIFT_MSG, shift(5000, 1.01)), 'impossible_score');
		assert.equal(rejectionCode(SHIFT_MSG, shift(5000, -0.01)), 'impossible_score');
		assert.equal(rejectionCode(SHIFT_MSG, shift(100, 7)), 'minigame_too_short', 'duration is checked first');
		assert.equal(rejectionCode(SHIFT_MSG, shift(5000, 0.5)), GENERIC_REASON, 'nothing implausible in the cmd');
		assert.equal(rejectionCode(SHIFT_MSG), GENERIC_REASON, 'no cmd');
		assert.equal(rejectionCode(SHIFT_MSG, { type: 'ageConfirm' }), GENERIC_REASON, 'not a shift');
	});

	it('unknown, empty, wrong-case and non-string messages fall back to "rejected"', () => {
		for (const m of ['', 'что-то новое', 'сезон закрыт', 'Сезон закрыт!', ' Сезон закрыт', 'Не хватает много 🪙', 'Не хватает 5 🪙 ещё', 'not enough credits', 'daily_limit_exceeded']) {
			assert.equal(rejectionCode(m), 'rejected', JSON.stringify(m));
		}
		assert.equal(rejectionCode(undefined), 'rejected');
		assert.equal(rejectionCode(null as never), 'rejected');
	});

	it('the reasons of the architecture doc are exactly the keys of this test', () => {
		assert.deepEqual([...REJECTION_REASONS].sort(), Object.keys(EXPECTED).sort());
	});

	it('drift guard: every `error: "<literal>"` in the vendored lib is covered by this test', () => {
		const lib = (p: string) => readFileSync(fileURLToPath(new URL('../../vendor/frontend/web/src/lib/' + p, import.meta.url)), 'utf8');
		const files = ['api/mock/MockGameApi.ts', 'sim/build.ts', 'sim/loan.ts', 'sim/economy.ts', 'sim/history.ts'];
		const missing = new Set<string>();
		let seen = 0;
		for (const f of files) {
			for (const m of lib(f).matchAll(/error: (['"`])((?:\\.|(?!\1).)*)\1/g)) {
				seen++;
				if (!known.has(norm(m[2]))) missing.add(`${f}: ${m[2]}`);
			}
		}
		assert.ok(seen > 40, `scanned ${seen} literals`);
		assert.deepEqual([...missing], [], 'new rejection message in vendor: add it to EXPECTED (and to REJECTION_RULES if it needs its own reason)');
	});
});

describe('command(): rejections carry {ok:false, error:<ru>, code}', () => {
	let base: StateObject;
	let now: number;
	before(async () => {
		({ state: base, now } = await buildTutorialLine(await newFactory(), T0));
	});
	const run = (cmd: Command, st: StateObject = base) => command(st, now + 1000, cmd);

	it('generic rule violation keeps the original message and is "rejected"', async () => {
		const r = await run({ type: 'renameFactory', name: 'x' });
		assert.deepEqual(r.result, { ok: false, error: 'Название от 2 символов', code: 'rejected' });
	});

	it('not_found: unknown order, unknown station, unknown building', async () => {
		assert.equal((await run({ type: 'acceptOrder', orderId: 'nope', dockId: 'nope' })).result.code, 'not_found');
		assert.equal((await run(shiftCmd(base, {}, 'nope'))).result.code, 'not_found');
		assert.equal((await run({ type: 'upgrade', buildingId: 'nope' })).result.code, 'not_found');
	});

	it('insufficient_funds: building without money', async () => {
		const poor = structuredClone(base) as { sim: { credits: number } };
		poor.sim.credits = 0;
		const r = await run({ type: 'place', kind: 'storage', x: 20, y: 12, rot: 0 }, poor as unknown as StateObject);
		assert.equal(r.result.ok, false);
		assert.match(r.result.error!, /^Не хватает \d+ 🪙$/);
		assert.equal(r.result.code, 'insufficient_funds');
	});

	it('season_closed: loan after the season end', async () => {
		const closed = structuredClone(base) as { season: { endsAt: number } };
		closed.season.endsAt = now;
		const r = await run({ type: 'takeLoan', amount: 100 }, closed as unknown as StateObject);
		assert.deepEqual(r.result, { ok: false, error: 'Сезон закрыт', code: 'season_closed' });
	});

	it('submitShift: minigame_too_short, impossible_score, wrong profession', async () => {
		const short = await run(shiftCmd(base, { durationMs: 399 }));
		assert.deepEqual(short.result, { ok: false, error: SHIFT_MSG, code: 'minigame_too_short' });
		assert.equal((await run(shiftCmd(base, { durationMs: 400 }))).result.ok, true, '400 ms is the boundary');
		assert.equal((await run(shiftCmd(base, { score: 1.2 }))).result.code, 'impossible_score');
		assert.equal((await run(shiftCmd(base, { score: -0.5 }))).result.code, 'impossible_score');
		assert.equal((await run(shiftCmd(base, { score: 1 }))).result.ok, true, 'score 1 is allowed');
		assert.equal((await run(shiftCmd(base, { profession: 'lab' }))).result.code, 'rejected');
		// a rejected shift does not burn the daily counter
		assert.equal(season(short.state).shiftsLeft, 5);
	});

	it('tick / undo / redo carry their own codes', async () => {
		assert.deepEqual((await run({ type: 'tick', from: 0, to: 1, events: [] })).result, { ok: false, error: 'tick_removed', code: 'tick_removed' });
		for (const type of ['undo', 'redo'] as const) {
			assert.deepEqual((await run({ type })).result, { ok: false, error: 'history_unavailable', code: 'history_unavailable' });
		}
	});

	it('successful commands have neither code nor error', async () => {
		const r = await run({ type: 'renameFactory', name: 'Квас-2' });
		assert.equal(r.result.ok, true);
		assert.ok(!('code' in r.result) && !('error' in r.result));
	});
});
