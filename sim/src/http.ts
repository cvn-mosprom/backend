// HTTP surface of the sim service (docs/ARCHITECTURE.md, "Sim service HTTP API"). node:http, no framework.

import { randomUUID } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Command } from '$lib/api/types';
import { COMMAND_TYPES, bootstrap, catchUp, command, snapshot, type PlayerInfo } from './engine';
import type { StateObject } from './host';

export const BODY_LIMIT_BYTES = 2 * 1024 * 1024;

export class HttpError extends Error {
	constructor(
		readonly status: number,
		readonly code: string,
		message: string
	) {
		super(message);
	}
}

const bad = (message: string) => new HttpError(400, 'bad_request', message);

// ---------- validation ----------

type Obj = Record<string, unknown>;
const isObj = (x: unknown): x is Obj => typeof x === 'object' && x !== null && !Array.isArray(x);
const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);

function needObj(x: unknown, path: string): Obj {
	if (!isObj(x)) throw bad(`${path} must be an object`);
	return x;
}

function needNow(b: Obj): number {
	const now = b.now;
	if (!isNum(now) || now <= 0) throw bad('now must be a positive number (ms since epoch)');
	return now;
}

/** Structural guard for the parts of MockState whose absence would crash the mock (everything else is opaque). */
function needState(x: unknown): StateObject {
	const s = needObj(x, 'state');
	const fail = (what: string) => bad(`state is not a factory state: ${what}`);
	if (s.v !== 1) throw fail('v must be 1');
	const sim = s.sim;
	if (!isObj(sim)) throw fail('sim');
	const layout = sim.layout;
	if (!isObj(layout) || !Array.isArray(layout.buildings) || !Array.isArray(layout.conveyors) || !isObj(layout.hangar)) throw fail('sim.layout');
	if (!isNum(sim.credits)) throw fail('sim.credits');
	if (!Array.isArray(sim.orders)) throw fail('sim.orders');
	for (const k of ['stock', 'shipped', 'collection', 'lastShipped'] as const) if (!isObj(sim[k])) throw fail(`sim.${k}`);
	if (!isObj(s.player)) throw fail('player');
	if (!isNum(s.lastTick) || !isNum(s.lastSeenReal) || !isNum(s.level)) throw fail('lastTick/lastSeenReal/level');
	const season = s.season;
	if (!isObj(season) || !isNum(season.startsAt) || !isNum(season.endsAt) || !isNum(season.shiftsLeft)) throw fail('season');
	if (!isObj(s.skills) || !isObj(s.skills.byProfession)) throw fail('skills');
	const d = s.dialogs;
	if (!isObj(d) || !Array.isArray(d.done) || !Array.isArray(d.factsSeen)) throw fail('dialogs');
	return s;
}

function needPlayer(x: unknown): PlayerInfo {
	const p = needObj(x, 'player');
	const id = typeof p.id === 'number' && Number.isFinite(p.id) ? String(p.id) : p.id;
	if (typeof id !== 'string' || id.length === 0 || id.length > 128) throw bad('player.id must be a non-empty string');
	if (typeof p.name !== 'string' || p.name.length === 0 || p.name.length > 256) throw bad('player.name must be a non-empty string');
	const out: PlayerInfo = { id, name: p.name };
	if (p.tgId !== undefined && p.tgId !== null) {
		if (!isNum(p.tgId) || !Number.isInteger(p.tgId)) throw bad('player.tgId must be an integer');
		out.tgId = p.tgId;
	}
	if (p.username !== undefined && p.username !== null) {
		if (typeof p.username !== 'string' || p.username.length > 256) throw bad('player.username must be a string');
		out.username = p.username;
	}
	return out;
}

function needCommand(x: unknown): Command {
	const c = needObj(x, 'cmd');
	if (typeof c.type !== 'string') throw bad('cmd.type must be a string');
	if (!COMMAND_TYPES.has(c.type)) throw bad(`unknown command type: ${c.type}`);
	return c as unknown as Command;
}

// ---------- routes ----------

type Handler = (body: Obj) => Promise<unknown>;

const routes: Record<string, Handler> = {
	'/v1/bootstrap': async (b) => {
		const now = needNow(b);
		if (b.state === undefined) throw bad('state is required (object or null)');
		if (b.state === null) {
			if (b.player === undefined) throw bad('player is required when state is null');
			return bootstrap(null, now, needPlayer(b.player));
		}
		const state = needState(b.state);
		if (b.player !== undefined && b.player !== null) needPlayer(b.player); // validated, not applied to an existing factory
		return bootstrap(state, now);
	},

	'/v1/command': async (b) => {
		const now = needNow(b);
		const state = needState(b.state);
		const cmd = needCommand(b.cmd);
		return command(state, now, cmd);
	},

	'/v1/advance': async (b) => {
		const now = needNow(b);
		const state = needState(b.state);
		let capHours: number | undefined;
		if (b.capHours !== undefined && b.capHours !== null) {
			if (!isNum(b.capHours) || b.capHours < 0) throw bad('capHours must be a number >= 0');
			capHours = b.capHours;
		}
		if (b.reason !== undefined && b.reason !== null && typeof b.reason !== 'string') throw bad('reason must be a string');
		const r = await catchUp(state, now, capHours);
		return { state: r.state, snapshot: r.snapshot, advancedMs: r.advancedMs };
	},

	'/v1/snapshot': async (b) => {
		const now = needNow(b);
		const state = needState(b.state);
		return { snapshot: await snapshot(state, now) };
	}
};

// ---------- plumbing ----------

function send(res: ServerResponse, status: number, payload: unknown, requestId: string) {
	const body = JSON.stringify(payload);
	res.writeHead(status, {
		'content-type': 'application/json; charset=utf-8',
		'content-length': Buffer.byteLength(body),
		'x-request-id': requestId
	});
	res.end(body);
}

const errorBody = (code: string, message?: string) => ({ error: message === undefined ? { code } : { code, message } });

/** Collect the body, enforcing the size limit (an oversized body is drained, not buffered, so the 413 can be delivered). */
function readBody(req: IncomingMessage, limit: number): Promise<Buffer> {
	return new Promise((resolve, reject) => {
		const declared = Number(req.headers['content-length']);
		let size = 0;
		let tooLarge = Number.isFinite(declared) && declared > limit;
		const chunks: Buffer[] = [];
		req.on('data', (chunk: Buffer) => {
			size += chunk.length;
			if (size > limit) tooLarge = true;
			if (!tooLarge) chunks.push(chunk);
			else chunks.length = 0;
		});
		req.on('end', () => (tooLarge ? reject(new HttpError(413, 'payload_too_large', `body exceeds ${limit} bytes`)) : resolve(Buffer.concat(chunks))));
		req.on('error', reject);
	});
}

export interface HandlerOptions {
	simCommit: string;
	log?: (line: Record<string, unknown>) => void;
}

export function createRequestHandler(opts: HandlerOptions) {
	const log = opts.log ?? (() => undefined);

	return async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
		const started = performance.now();
		const hdr = req.headers['x-request-id'];
		const requestId = (typeof hdr === 'string' && hdr.length > 0 && hdr.length <= 128 ? hdr : undefined) ?? randomUUID();
		const path = (req.url ?? '/').split('?')[0];
		let status = 200;
		try {
			if (path === '/health') {
				if (req.method !== 'GET' && req.method !== 'HEAD') throw new HttpError(405, 'method_not_allowed', 'use GET');
				return send(res, 200, { status: 'ok', service: 'sim', simCommit: opts.simCommit }, requestId);
			}
			const route = Object.hasOwn(routes, path) ? routes[path] : undefined;
			if (!route) throw new HttpError(404, 'not_found', `no such endpoint: ${path}`);
			if (req.method !== 'POST') throw new HttpError(405, 'method_not_allowed', 'use POST');

			const raw = await readBody(req, BODY_LIMIT_BYTES);
			let body: unknown;
			try {
				body = JSON.parse(raw.toString('utf8'));
			} catch {
				throw bad('body is not valid JSON');
			}
			if (!isObj(body)) throw bad('body must be a JSON object');
			send(res, 200, await route(body), requestId);
		} catch (e) {
			if (e instanceof HttpError) {
				status = e.status;
				send(res, e.status, errorBody(e.code, e.message), requestId);
			} else {
				status = 500;
				log({ level: 'error', msg: 'unhandled error', path, requestId, err: e instanceof Error ? (e.stack ?? e.message) : String(e) });
				if (!res.headersSent) send(res, 500, errorBody('internal_error', 'internal error'), requestId);
				else res.destroy();
			}
		} finally {
			log({ level: 'debug', msg: 'request', method: req.method, path, status, ms: Math.round(performance.now() - started), requestId });
		}
	};
}
