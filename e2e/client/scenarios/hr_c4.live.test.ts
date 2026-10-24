// e2e клиента CRM против живого стека, часть C4: кандидаты, карточка, PATCH с версией, заметки, сообщения, воронка.
// Запуск тот же, что у hr.live.test.ts (стек с DEV_AUTH=1, свой tg id в HR_ADMIN_TG_IDS или строка admin в hr_users):
//   HR_E2E_API=http://localhost:8000 HR_E2E_ADMIN_TG=<id> pnpm vitest run src/lib/hr/hr.c4.live.test.ts
// Через Caddy с самоподписанным сертификатом: HR_E2E_API=https://localhost NODE_TLS_REJECT_UNAUTHORIZED=0.
// Без переменных тест пропускается. Кандидата заводит сам через публичную анкету лендинга (игры у него нет) и в конце
// отзывает согласие: из CRM он пропадает.

import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { HttpHrApi } from '$lib/hr/api';
import { COMPETENCIES, STATUSES } from '$lib/hr/labels';
import { HrApiError, type CandidateRow } from '$lib/hr/types';

const env = (globalThis as unknown as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};
const API = (env.HR_E2E_API ?? '').replace(/\/$/, '');
const ADMIN = Number(env.HR_E2E_ADMIN_TG ?? 0);
const live = !!API && ADMIN > 0;

const as = (tg: number) => new HttpHrApi(API, () => `dev ${tg}:e2e-hr`);
const rnd = () => 9_000_000_000 + Math.floor(Math.random() * 900_000_000);
async function code(p: Promise<unknown>): Promise<string> {
	try {
		await p;
		return 'ok';
	} catch (e) {
		return e instanceof HrApiError ? `${e.status} ${e.code}` : String(e);
	}
}
const uuid = () => crypto.randomUUID();

describe.skipIf(!live)('CRM против живого API (C4)', () => {
	const admin = as(ADMIN);
	const analystTg = rnd();
	const hrTg = rnd();
	const surname = `Смоуков${Math.floor(Math.random() * 1e6)}`;
	const submissionId = uuid();
	const receiptToken = uuid();
	let leadId = 0;
	let hrId = 0;

	beforeAll(async () => {
		const born = new Date();
		born.setFullYear(born.getFullYear() - 22);
		const r = await fetch(`${API}/api/leads/public`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				fullName: `Иван ${surname}`, birthDate: born.toISOString().slice(0, 10), phone: '+79990001122', university: 'Тестовый вуз',
				roleId: 'lab', department: 'lab', practiceType: 'paid', source: 'c4_e2e', consent: true, consentTextVersion: 'e2e',
				submissionId, receiptToken
			})
		});
		expect(r.status).toBe(201);
		leadId = (await r.json()).id;
		hrId = (await admin.addUser(hrTg, 'hr')).id;
		await admin.addUser(analystTg, 'analyst');
	});

	afterAll(async () => {
		await fetch(`${API}/api/leads/public/${submissionId}`, { method: 'DELETE', headers: { Authorization: `Bearer ${receiptToken}` } });
		await admin.patchUser(hrId, { active: false }).catch(() => {});
	});

	it('список: форма страницы, поиск, фильтры и сортировки принимаются', async () => {
		const page = await admin.candidates({ sort: 'fresh', q: surname });
		expect(page.total).toBe(1);
		const row = page.items[0];
		expect(row).toMatchObject({ id: leadId, fullName: `Иван ${surname}`, piiMasked: false, status: 'new', roleId: 'lab', source: 'c4_e2e', assignee: null, seasonRank: null, fitBest: null, achievementPoints: 0 });
		expect(Object.keys(row.competency).sort()).toEqual([...COMPETENCIES].sort());
		expect(row.version).toBeGreaterThanOrEqual(1);
		for (const sort of ['fresh', 'points', 'fit', 'rank'] as const) {
			const p = await admin.candidates({ sort, status: ['new', 'screening'], profession: 'lab', minPoints: 0, minFit: 0, limit: 2 });
			expect(p.items.length).toBeLessThanOrEqual(2);
			expect(p.total).toBeGreaterThanOrEqual(p.items.length);
		}
		const withComp = await admin.candidates({ sort: 'points', q: surname, competency: { key: 'speed', min: 1 } });
		expect(withComp.total).toBe(0); // лендинговый кандидат без игры: компетенции нулевые
		expect((await admin.candidates({ sort: 'fresh', q: surname, assignee: 'none' })).total).toBe(1);
		expect((await admin.candidates({ sort: 'fresh', q: surname, assignee: 'me' })).total).toBe(0);
	});

	it('курсор: обход страниц без повторов', async () => {
		const first = await admin.candidates({ sort: 'fresh', limit: 2 });
		if (!first.nextCursor) return; // в стеке меньше трёх кандидатов
		const second = await admin.candidates({ sort: 'fresh', limit: 2, cursor: first.nextCursor });
		const ids = new Set(first.items.map((c) => c.id));
		expect(second.items.every((c) => !ids.has(c.id))).toBe(true);
		expect(second.total).toBe(first.total);
		expect(await code(admin.candidates({ sort: 'points', limit: 2, cursor: first.nextCursor }))).toBe('422 validation_error');
	});

	it('карточка: анкета, игровой профиль, достижения, история, просмотр пишется', async () => {
		const c = await admin.candidate(leadId);
		expect(c).toMatchObject({ id: leadId, phone: '+79990001122', canMessage: false, practiceType: 'paid', fit: [], notes: [] });
		expect(c.game).toEqual({ level: 0, stars: 0, quality: 0, shiftsTotal: 0, quizScore: null, byProfession: [] });
		expect(c.achievements.length).toBeGreaterThan(30);
		expect(c.achievements.every((a) => a.earnedAt === null)).toBe(true);
		expect(c.history.some((e) => e.kind === 'view')).toBe(true);
		expect(c.history.at(-1)).toMatchObject({ kind: 'status', payload: { to: 'new' } });
		expect(await code(admin.candidate(2_000_000_000))).toBe('404 lead_not_found');
	});

	it('PATCH: статус, назначенный, теги; устаревшая версия — 409', async () => {
		const v0 = (await admin.candidate(leadId)).version;
		const r = await admin.patch(leadId, { status: 'interview', tags: ['e2e'], assigneeId: hrId, version: v0 });
		expect(r).toMatchObject({ status: 'interview', tags: ['e2e'], version: v0 + 1 });
		expect(r.assignee?.id).toBe(hrId);
		expect(await code(admin.patch(leadId, { status: 'offer', version: v0 }))).toBe('409 version_conflict');
		expect(await code(admin.patch(leadId, { status: 'bogus' as never, version: v0 + 1 }))).toBe('422 validation_error');
		const back = await admin.patch(leadId, { assigneeId: null, version: v0 + 1 });
		expect(back.assignee).toBeNull();
		const c = await admin.candidate(leadId);
		const kinds = c.history.map((e) => e.kind);
		expect(kinds).toEqual(expect.arrayContaining(['status', 'assign', 'tag']));
		expect(c.history.find((e) => e.kind === 'status' && e.payload.to === 'interview')?.payload).toMatchObject({ from: 'new', to: 'interview' });
	});

	it('заметки и сообщения: заметка в карточке, лендинговому кандидату написать нельзя', async () => {
		const n = await admin.addNote(leadId, 'Позвонить во вторник');
		expect(n).toMatchObject({ text: 'Позвонить во вторник' });
		expect((await admin.candidate(leadId)).notes.map((x) => x.text)).toContain('Позвонить во вторник');
		expect(await code(admin.message(leadId, 'interview', 'Привет'))).toBe('409 bot_not_started');
		expect(await code(admin.addNote(leadId, '   '))).toBe('422 validation_error');
	});

	it('воронка: семь колонок по порядку, кандидат в своей колонке', async () => {
		const p = await admin.pipeline();
		expect(p.columns.map((c) => c.status)).toEqual(STATUSES);
		const col = p.columns.find((c) => c.status === 'interview')!;
		expect(col.count).toBeGreaterThanOrEqual(1);
		expect(col.items.length).toBeLessThanOrEqual(50);
		expect(col.items.some((c: CandidateRow) => c.id === leadId)).toBe(true);
		const lab = await admin.pipeline({ profession: 'lab', assignee: 'none' });
		expect(lab.columns.find((c) => c.status === 'interview')!.items.some((c) => c.id === leadId)).toBe(true);
	});

	it('аналитик читает без ПДн и ничего не пишет; hr пишет', async () => {
		const analyst = as(analystTg);
		const row = (await analyst.candidates({ sort: 'fresh' })).items.find((c) => c.id === leadId)!;
		expect(row).toMatchObject({ piiMasked: true, fullName: `Кандидат #${leadId}` });
		expect((await analyst.candidates({ sort: 'fresh', q: surname })).total).toBe(0); // по имени аналитик не ищет
		const c = await analyst.candidate(leadId);
		expect(c).toMatchObject({ piiMasked: true, phone: null, email: null, telegram: null, birthDate: null, notes: [] });
		expect(JSON.stringify(c)).not.toContain(surname);
		expect((await analyst.pipeline()).columns).toHaveLength(7);
		expect(await code(analyst.patch(leadId, { status: 'offer', version: row.version }))).toBe('403 forbidden');
		expect(await code(analyst.addNote(leadId, 'x'))).toBe('403 forbidden');
		expect(await code(analyst.message(leadId, 'offer', 'x'))).toBe('403 forbidden');
		const hr = as(hrTg);
		expect((await hr.candidate(leadId)).fullName).toBe(`Иван ${surname}`);
		const v = (await hr.candidate(leadId)).version;
		expect((await hr.patch(leadId, { status: 'reserve', version: v })).status).toBe('reserve');
		expect(await code(as(rnd()).candidates({ sort: 'fresh' }))).toBe('403 forbidden');
	});

	it('после отзыва согласия кандидата в CRM нет', async () => {
		const r = await fetch(`${API}/api/leads/public/${submissionId}`, { method: 'DELETE', headers: { Authorization: `Bearer ${receiptToken}` } });
		expect(r.status).toBe(204);
		expect(await code(admin.candidate(leadId))).toBe('404 lead_not_found');
		expect((await admin.candidates({ sort: 'fresh', q: surname })).total).toBe(0);
	});
});
