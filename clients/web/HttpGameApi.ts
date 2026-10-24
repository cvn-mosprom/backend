// HttpGameApi: drop-in GameApi (web/src/lib/api/types.ts) over the FastAPI backend (S2 contract, docs/ARCHITECTURE.md).
// Copy to web/src/lib/api/HttpGameApi.ts. Imports use the `$lib` alias, which works both in SvelteKit and in
// clients/web/tsconfig.json (typecheck).
//
// Behaviour in one paragraph:
//  * auth: `Authorization: tma <initData>` from Telegram WebApp, dev fallback `dev <id>` (server needs DEV_AUTH=1);
//  * bootstrap(): first call -> GET /api/bootstrap; every later call is a passive poll -> GET /api/factory/me,
//    throttled (`minPollMs`), so the frontend's existing 1 s poll loop cannot trip the 10/min bootstrap limiter;
//  * command(): POST /api/command {version, idempotency_key, cmd}. Commands are serialised. One fresh UUID key per
//    logical command, reused for every retry (replay is checked on the server BEFORE the version):
//      409 version_conflict -> GET /api/factory/me (new version) -> retry ONCE with the same key,
//      429 -> wait Retry-After once -> retry, network error / 502-504 -> retry (max 2) with the same key,
//      422 -> {ok:false, error: details.message, snapshot: details.snapshot};
//  * `tick` is never sent (removed server-side): returns {ok:false, error:'tick_removed'} locally;
//  * undo/redo are not sent while features.undo is false (server would answer 422 history_unavailable);
//  * onResume() / visibilitychange -> GET /api/factory/me resync (no 409 can come from it).

import type {
	Command,
	CommandResult,
	GameApi,
	GameSnapshot,
	LeadForm,
	Leaderboard,
	LeaderboardTab,
	Player,
	SeasonSummary,
	Factory
} from '$lib/api/types';

export interface ServerFeatures {
	/** Server-side undo/redo (S1U migration). While false: hide the undo/redo buttons and Ctrl+Z. */
	undo: boolean;
	/** Server-side order ETA moments in the snapshot (after S1 merge). */
	serverOrderEta: boolean;
}

export interface SeasonInfo {
	id: number;
	startsAt: number;
	endsAt: number;
}

/** Version of the privacy-policy text shown next to the consent checkbox (LeadForm.svelte / PrivacyText.svelte). Bump it with the text. */
export const CONSENT_TEXT_VERSION = '2026-10-v1';

/** Fields of the case form beyond the vendored LeadForm (all optional on the wire; ISO dates YYYY-MM-DD). */
export interface LeadFormExtras {
	birthDate?: string;
	phone?: string;
	email?: string;
	telegram?: string;
	direction?: string;
	/** Profession id (kvasovar|operator|adjuster|lab|storekeeper|forklift|kipia); defaults to LeadForm.roleId. */
	vacancy?: string;
	readyFrom?: string;
	readiness?: 'now' | 'season' | 'later';
	practiceType?: 'paid' | 'free' | 'job';
	consentTextVersion?: string;
}

/** GET /api/leads/me (the owner's view; no HR notes). */
export interface LeadView {
	id: number;
	seasonId: number;
	status: 'new' | 'contacted' | 'interview' | 'rejected' | 'hired';
	tags: string[];
	fullName: string;
	birthDate: string | null;
	phone: string | null;
	email: string | null;
	telegram: string | null;
	university: string;
	direction: string | null;
	course: string | null;
	vacancy: string | null;
	readyFrom: string | null;
	readiness: string | null;
	practiceType: string | null;
	consent: boolean;
	consentAt: string;
	consentTextVersion: string;
	consentRevokedAt: string | null;
	bestProfession: string | null;
	seasonPlace: number | null;
	quizScore: number | null;
	createdAt: string;
	updatedAt: string;
}

export interface QuizProgress {
	score: number;
	answered: number;
	total: number;
	completed: boolean;
}

export interface QuizQuestions {
	questions: { id: string; text: string; options: string[] }[];
	progress: QuizProgress & { answeredIds: string[] };
}

export interface QuizResult extends QuizProgress {
	results: { questionId: string; correct: boolean; correctAnswer: number; explanation: string }[];
}

export interface HttpGameApiOptions {
	/** Origin + optional prefix, no trailing slash. '' = same origin (default). */
	baseUrl?: string;
	/** Override the Authorization header value (tests, non-Telegram embeds). */
	authHeader?: () => string;
	/** Dev fallback telegram id (`dev <id>`); default: persisted random id in localStorage. */
	devTgId?: number | string;
	/** Custom fetch (tests). */
	fetch?: typeof fetch;
	/** Resync on visibilitychange -> visible. Default true. */
	autoResume?: boolean;
	/** Min interval between passive polls (bootstrap() after the first call), ms. Default 4000. */
	minPollMs?: number;
	/** Per-request timeout, ms. Default 15000. */
	requestTimeoutMs?: number;
	/** Called whenever a snapshot arrives outside a command() call (poll / resume), so the UI store can update. */
	onSnapshot?: (s: GameSnapshot) => void;
}

export class ApiError extends Error {
	constructor(
		public status: number,
		public code: string,
		message: string,
		public details: Record<string, unknown> = {}
	) {
		super(message);
		this.name = 'ApiError';
	}
}

class NetworkError extends Error {}

interface HttpResult {
	status: number;
	headers: Headers;
	body: unknown;
}

interface Envelope {
	version?: number;
	serverTime?: number;
	snapshot?: GameSnapshot | null;
	season?: SeasonInfo;
	features?: Partial<ServerFeatures>;
	degraded?: boolean;
	result?: { toast?: string; rewards?: { credits?: number; stars?: number; xp?: number; unlocked?: string[] } };
	replayed?: boolean;
}

const MAX_NET_RETRIES = 2;
const NET_BACKOFF_MS = [300, 900];
const UNDO_OFF = 'Отмена пока недоступна';
const DEV_ID_KEY = 'mosprom.devTgId';

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export function uuid(): string {
	const c = (globalThis as { crypto?: Crypto }).crypto;
	if (c?.randomUUID) return c.randomUUID();
	// non-secure context fallback (http:// dev servers): 36 chars, fine for the 8..64 key rule
	const b = new Uint8Array(16);
	if (c?.getRandomValues) c.getRandomValues(b);
	else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
	b[6] = (b[6] & 0x0f) | 0x40;
	b[8] = (b[8] & 0x3f) | 0x80;
	const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
	return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function errorOf(r: HttpResult): ApiError {
	const e = (r.body as { error?: { code?: string; message?: string; details?: Record<string, unknown> } } | null)?.error;
	return new ApiError(r.status, e?.code ?? 'http_error', e?.message ?? `HTTP ${r.status}`, e?.details ?? {});
}

function retryAfterMs(h: Headers): number {
	const v = h.get('Retry-After');
	let s = 1;
	if (v) {
		const n = Number(v);
		if (Number.isFinite(n)) s = n;
		else {
			const d = Date.parse(v);
			if (!Number.isNaN(d)) s = (d - Date.now()) / 1000;
		}
	}
	return Math.min(10_000, Math.max(500, Math.round(s * 1000)));
}

let active: HttpGameApi | undefined;

/** Resync hook for "returned from background" (also wired to visibilitychange automatically). */
export function onResume(): Promise<GameSnapshot | undefined> {
	return active ? active.resume() : Promise.resolve(undefined);
}

export class HttpGameApi implements GameApi {
	private readonly base: string;
	private readonly fetchImpl: typeof fetch;
	private readonly minPollMs: number;
	private readonly timeoutMs: number;
	private readonly onSnapshot?: (s: GameSnapshot) => void;
	private readonly opts: HttpGameApiOptions;

	private booted = false;
	private _version = 0;
	private _snapshot: GameSnapshot | null = null;
	private _features: ServerFeatures = { undo: false, serverOrderEta: false };
	private _season: SeasonInfo | null = null;
	private _degraded = false;
	private clockBase = 0;
	private clockPerf = 0;
	private lastSyncAt = 0;
	private queue: Promise<unknown> = Promise.resolve();
	private inflightSync: Promise<GameSnapshot> | null = null;
	private booting: Promise<GameSnapshot> | null = null;
	private _lastLeadError: ApiError | null = null;

	constructor(opts: HttpGameApiOptions = {}) {
		this.opts = opts;
		this.base = (opts.baseUrl ?? '').replace(/\/+$/, '');
		this.fetchImpl = opts.fetch ?? ((...a) => globalThis.fetch(...a));
		this.minPollMs = opts.minPollMs ?? 4000;
		this.timeoutMs = opts.requestTimeoutMs ?? 15_000;
		this.onSnapshot = opts.onSnapshot;
		active = this;
		if (opts.autoResume !== false && typeof document !== 'undefined') {
			document.addEventListener('visibilitychange', () => {
				if (document.visibilityState === 'visible') void this.resume().catch(() => undefined);
			});
		}
	}

	// ---------- public state ----------

	/** Last known factory version (sent with every command). */
	get version(): number { return this._version; }
	/** Server flags; `features.undo === false` -> hide undo/redo. */
	get features(): ServerFeatures { return this._features; }
	get season(): SeasonInfo | null { return this._season; }
	/** True when the last factory/me answered from the stored snapshot because the sim was down. */
	get degraded(): boolean { return this._degraded; }
	get snapshot(): GameSnapshot | null { return this._snapshot; }

	/** Server time: serverTime of the last snapshot + local elapsed. */
	now(): number {
		if (!this.clockBase) return Date.now();
		return this.clockBase + (performance.now() - this.clockPerf);
	}

	// ---------- GameApi ----------

	async bootstrap(): Promise<GameSnapshot> {
		if (!this.booted) {
			this.booting ??= this.getJson<Envelope>('/api/bootstrap')
				.then((env) => {
					this.apply(env, true);
					this.booted = true;
					this.lastSyncAt = Date.now();
					return this._snapshot!;
				})
				.finally(() => (this.booting = null));
			return this.booting;
		}
		// passive poll: the frontend calls bootstrap() every second -> factory/me, throttled
		if (this._snapshot && Date.now() - this.lastSyncAt < this.minPollMs) return this._snapshot;
		return this.sync();
	}

	command(cmd: Command): Promise<CommandResult> {
		if (cmd.type === 'tick') return Promise.resolve({ ok: false, error: 'tick_removed' });
		if ((cmd.type === 'undo' || cmd.type === 'redo') && !this._features.undo) {
			return Promise.resolve({ ok: false, error: UNDO_OFF, snapshot: this._snapshot ?? undefined });
		}
		const run = this.queue.then(() => this.runCommand(cmd));
		this.queue = run.catch(() => undefined);
		return run;
	}

	/** GET /api/season/leaderboard. Same shape as the mock's board(): rows, me, total, inviteCutoff, gapTo51, universities.
	 *  Errors: 422 invalid_tab, 429 (Retry-After honoured once by sendJson), 401/5xx -> ApiError. */
	async leaderboard(tab: LeaderboardTab): Promise<Leaderboard> {
		return this.getJson<Leaderboard>('/api/season/leaderboard', { tab });
	}

	/** GET /api/factory/:id: the PUBLIC cut {player, factory:{name, level, stars, layout, updatedAt}}. The frontend's
	 *  Factory type also wants the private fields; they are filled with neutral values (the mock did the same for bots:
	 *  credits 0, xp 0, nothing shipped). 404 player_not_found -> ApiError(404). */
	async visit(playerId: string): Promise<{ player: Pick<Player, 'id' | 'name' | 'university'>; factory: Factory }> {
		const v = await this.getJson<{
			player: Pick<Player, 'id' | 'name' | 'university'>;
			factory: { name: string; level: number; stars: number; layout: Factory['layout']; updatedAt: number };
		}>(`/api/factory/${encodeURIComponent(playerId)}`);
		const f = v.factory;
		return {
			player: v.player,
			factory: {
				name: f.name, layout: f.layout, level: f.level, stars: f.stars, credits: 0, xp: 0,
				shipped: {}, collection: {}, rejected: 0, lastTick: f.updatedAt
			}
		};
	}

	async seasonSummary(): Promise<SeasonSummary> {
		return this.getJson<SeasonSummary>('/api/season/summary');
	}

	/** POST /api/leads. The vendored form sends fullName/contact/university/course/roleId/consent; the case form's extra
	 *  fields (birthDate, phone, email, telegram, direction, vacancy, readyFrom, readiness, practiceType) are passed through
	 *  when the form provides them. `contact` is mapped to phone/email/telegram by the server (by format).
	 *  `consentTextVersion` is the version of the privacy text the form shows (server: 403 consent_required without it).
	 *  ok:false = form-level failure, see `lastLeadError` (403 age_required|consent_required, 422 validation_error);
	 *  409 lead_already_exists counts as ok (the application is on file; also what a retry after a lost response gets). */
	async submitLead(form: LeadForm & LeadFormExtras): Promise<{ ok: boolean }> {
		this._lastLeadError = null;
		try {
			await this.sendJson('POST', '/api/leads', { ...form, consentTextVersion: form.consentTextVersion ?? CONSENT_TEXT_VERSION });
			return { ok: true };
		} catch (e) {
			if (e instanceof ApiError && [403, 409, 422].includes(e.status)) {
				this._lastLeadError = e;
				return { ok: e.code === 'lead_already_exists' };
			}
			throw e;
		}
	}

	/** Why the last submitLead() answered ok:false (or that the lead already existed); null after a clean success. */
	get lastLeadError(): ApiError | null { return this._lastLeadError; }

	/** GET /api/leads/me: the player's latest lead (their own data, incl. tags/status/consentRevokedAt), null when none. */
	async myLead(): Promise<LeadView | null> {
		try {
			return await this.getJson<LeadView>('/api/leads/me');
		} catch (e) {
			if (e instanceof ApiError && e.status === 404) return null;
			throw e;
		}
	}

	/** DELETE /api/leads/me: revoke the consent (152-FZ). Idempotent; false when there is no lead. */
	async revokeLead(): Promise<boolean> {
		try {
			await this.sendJson('DELETE', '/api/leads/me');
			return true;
		} catch (e) {
			if (e instanceof ApiError && e.status === 404) return false;
			throw e;
		}
	}

	/** GET /api/quiz/questions: the bank without answers + the player's progress. */
	quizQuestions(): Promise<QuizQuestions> {
		return this.getJson<QuizQuestions>('/api/quiz/questions');
	}

	/** POST /api/quiz/answer: a batch, checked on the server. Throws ApiError: 422 unknown_question|invalid_answer_format,
	 *  409 quiz_already_completed (answers are final). */
	quizAnswer(answers: { questionId: string; answer: number }[]): Promise<QuizResult> {
		return this.sendJson<QuizResult>('POST', '/api/quiz/answer', { answers });
	}

	/** POST /api/events {events:[{name, props?, ts}]} (<= 50, names from analytics.ts). `ts` is clamped to the server clock
	 *  (+5 s): the server rejects a batch with an event more than 60 s ahead, which a skewed phone clock would trigger.
	 *  4xx (a poison batch) is dropped; network errors, 429 and 5xx throw, so analytics.ts keeps the batch and retries. */
	async track(events: { name: string; props?: Record<string, unknown>; ts: number }[]): Promise<void> {
		if (!events.length) return;
		const max = this.now() + 5000;
		const batch = events.slice(0, 50).map((e) => ({ ...e, ts: Math.min(e.ts, max) }));
		try {
			await this.sendJson('POST', '/api/events', { events: batch });
		} catch (e) {
			if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.status !== 429) {
				console.warn('analytics batch dropped', e.code);
				return;
			}
			throw e;
		}
	}

	// ---------- resync ----------

	/** GET /api/factory/me: lazy catch-up on the server, never 409. Concurrent calls share one request. */
	sync(): Promise<GameSnapshot> {
		this.inflightSync ??= this.fetchMe(false).finally(() => (this.inflightSync = null));
		return this.inflightSync;
	}

	/** Return from background: always hits the server (ignores the poll throttle) and notifies onSnapshot. */
	async resume(): Promise<GameSnapshot> {
		const s = await this.sync();
		this.onSnapshot?.(s);
		return s;
	}

	private async fetchMe(force: boolean): Promise<GameSnapshot> {
		const env = await this.getJson<Envelope>('/api/factory/me');
		this.apply(env, force);
		this.lastSyncAt = Date.now();
		return this._snapshot!;
	}

	// ---------- command pipeline ----------

	private async runCommand(cmd: Command): Promise<CommandResult> {
		if (!this.booted) await this.bootstrap();
		const key = uuid(); // one logical command = one key for every retry
		let netTries = 0;
		let resynced = false;
		let throttled = false;
		for (;;) {
			let r: HttpResult;
			try {
				r = await this.http('POST', '/api/command', { version: this._version, idempotency_key: key, cmd });
			} catch {
				if (netTries < MAX_NET_RETRIES) {
					await sleep(NET_BACKOFF_MS[netTries++]);
					continue;
				}
				return this.fail('Нет связи с сервером, попробуй ещё раз');
			}

			if (r.status === 200) return this.accepted(r.body as Envelope);

			const err = errorOf(r);
			if (r.status === 409 && err.code === 'version_conflict') {
				// state moved on (other tab / device): resync, then retry the same command once with the same key
				try {
					await this.fetchMe(true);
				} catch {
					return this.fail('Нет связи с сервером, попробуй ещё раз');
				}
				if (resynced) return this.fail('Цех изменился, повтори действие', this._snapshot);
				resynced = true;
				continue;
			}
			if (r.status === 429) {
				if (throttled) return this.fail('Слишком часто, подожди секунду', this._snapshot);
				throttled = true;
				await sleep(retryAfterMs(r.headers));
				continue;
			}
			if (r.status === 422) return this.rejected(err);
			if ([502, 503, 504].includes(r.status) && netTries < MAX_NET_RETRIES) {
				await sleep(NET_BACKOFF_MS[netTries++]); // sim/db hiccup: same key, safe to repeat
				continue;
			}
			return this.fail(err.message || 'Ошибка сервера', this._snapshot);
		}
	}

	private accepted(env: Envelope): CommandResult {
		// buildEnd returns snapshot:null and the unchanged version; replays return the current version + fresh snapshot
		if (typeof env.version === 'number') this._version = env.version;
		if (env.snapshot) this.setSnapshot(env.snapshot, env.serverTime);
		const snapshot = this._snapshot;
		if (!snapshot) return this.fail('Нет данных цеха');
		return { ok: true, snapshot, toast: env.result?.toast, rewards: env.result?.rewards };
	}

	private rejected(err: ApiError): CommandResult {
		const d = err.details as { message?: string; version?: number; snapshot?: GameSnapshot };
		if (typeof d.version === 'number') this._version = d.version;
		if (d.snapshot) this.setSnapshot(d.snapshot);
		return { ok: false, error: d.message ?? err.message, snapshot: d.snapshot ?? this._snapshot ?? undefined };
	}

	private fail(error: string, snapshot?: GameSnapshot | null): CommandResult {
		return { ok: false, error, snapshot: snapshot ?? undefined };
	}

	// ---------- state ----------

	/** Merge a bootstrap / factory-me envelope. Unforced: ignore responses older than what we already have. */
	private apply(env: Envelope, force: boolean) {
		const snap = env.snapshot;
		if (!snap) return;
		const v = typeof env.version === 'number' ? env.version : this._version;
		const t = env.serverTime ?? snap.serverTime;
		const stale =
			!force && this._snapshot !== null && (v < this._version || (v === this._version && t < this._snapshot.serverTime));
		if (env.features) this._features = { ...this._features, ...env.features };
		if (env.season) this._season = env.season;
		if (typeof env.degraded === 'boolean') this._degraded = env.degraded;
		if (stale) return; // a slower passive poll must not roll the UI back
		this._version = v;
		this.setSnapshot(snap, env.serverTime);
	}

	private setSnapshot(s: GameSnapshot, serverTime?: number) {
		this._snapshot = s;
		this.clockBase = serverTime ?? s.serverTime;
		this.clockPerf = performance.now();
	}

	// ---------- transport ----------

	private auth(): string {
		if (this.opts.authHeader) return this.opts.authHeader();
		const initData = (globalThis as { Telegram?: { WebApp?: { initData?: string } } }).Telegram?.WebApp?.initData;
		if (initData) return `tma ${initData}`;
		return `dev ${this.devId()}`;
	}

	private devId(): string {
		if (this.opts.devTgId !== undefined) return String(this.opts.devTgId);
		try {
			let id = localStorage.getItem(DEV_ID_KEY);
			if (!id) {
				id = String(100_000_000 + Math.floor(Math.random() * 900_000_000));
				localStorage.setItem(DEV_ID_KEY, id);
			}
			return id;
		} catch {
			return '100000001';
		}
	}

	private async http(method: 'GET' | 'POST' | 'DELETE', path: string, body?: unknown, query?: Record<string, string>): Promise<HttpResult> {
		const qs = query ? `?${new URLSearchParams(query)}` : '';
		const ctl = new AbortController();
		const timer = setTimeout(() => ctl.abort(), this.timeoutMs);
		try {
			const res = await this.fetchImpl(`${this.base}${path}${qs}`, {
				method,
				headers: {
					Authorization: this.auth(),
					Accept: 'application/json',
					...(body !== undefined ? { 'Content-Type': 'application/json' } : {})
				},
				body: body !== undefined ? JSON.stringify(body) : undefined,
				signal: ctl.signal,
				keepalive: path === '/api/events'
			});
			const text = await res.text();
			let parsed: unknown = null;
			if (text) {
				try {
					parsed = JSON.parse(text);
				} catch {
					parsed = null;
				}
			}
			return { status: res.status, headers: res.headers, body: parsed };
		} catch {
			throw new NetworkError('network');
		} finally {
			clearTimeout(timer);
		}
	}

	/** GET/POST with the passive-request policy: retry network errors / 502-504 up to twice, honour Retry-After once. */
	private async sendJson<T>(method: 'GET' | 'POST' | 'DELETE', path: string, body?: unknown, query?: Record<string, string>): Promise<T> {
		let netTries = 0;
		let throttled = false;
		for (;;) {
			let r: HttpResult;
			try {
				r = await this.http(method, path, body, query);
			} catch (e) {
				if (netTries < MAX_NET_RETRIES) {
					await sleep(NET_BACKOFF_MS[netTries++]);
					continue;
				}
				throw new ApiError(0, 'network_error', e instanceof Error ? e.message : 'network');
			}
			if (r.status >= 200 && r.status < 300) return r.body as T;
			if (r.status === 429 && !throttled) {
				throttled = true;
				await sleep(retryAfterMs(r.headers));
				continue;
			}
			if ([502, 503, 504].includes(r.status) && netTries < MAX_NET_RETRIES) {
				await sleep(NET_BACKOFF_MS[netTries++]);
				continue;
			}
			throw errorOf(r);
		}
	}

	private getJson<T>(path: string, query?: Record<string, string>): Promise<T> {
		return this.sendJson<T>('GET', path, undefined, query);
	}
}
