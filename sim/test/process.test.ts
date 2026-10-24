// Process-level smoke: the real entry point (cluster primary + workers), from source via tsx and from the esbuild bundle.

import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import { workerCount } from '../src/config';
import { PLAYER, T0 } from './helpers';

const root = fileURLToPath(new URL('..', import.meta.url));
const dist = root + 'dist/server.mjs';

describe('workerCount (SIM_WORKERS)', () => {
	it('defaults to 2, clamps to 4, 0 means in-process, garbage falls back to 2', () => {
		assert.equal(workerCount(undefined), 2);
		assert.equal(workerCount(''), 2);
		assert.equal(workerCount('3'), 3);
		assert.equal(workerCount('4'), 4);
		assert.equal(workerCount('99'), 4);
		assert.equal(workerCount('0'), 0);
		assert.equal(workerCount('-1'), 2);
		assert.equal(workerCount('abc'), 2);
	});
});

async function freePort(): Promise<number> {
	return new Promise((resolve, reject) => {
		const s = createServer();
		s.once('error', reject);
		s.listen(0, '127.0.0.1', () => {
			const port = (s.address() as { port: number }).port;
			s.close(() => resolve(port));
		});
	});
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function until<T>(what: string, fn: () => T | undefined | false | Promise<T | undefined | false>, ms = 20_000): Promise<T> {
	const end = Date.now() + ms;
	for (;;) {
		try {
			const v = await fn();
			if (v) return v;
		} catch {
			/* not yet */
		}
		if (Date.now() > end) throw new Error(`timeout waiting for ${what}`);
		await sleep(100);
	}
}

const alive = (pid: number) => {
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
};

function launch(args: string[], env: Record<string, string>) {
	const child: ChildProcess = spawn(process.execPath, args, { cwd: root, env: { ...process.env, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
	const lines: Record<string, any>[] = [];
	let buf = '';
	child.stdout!.on('data', (d: Buffer) => {
		buf += d.toString();
		let i: number;
		while ((i = buf.indexOf('\n')) >= 0) {
			const line = buf.slice(0, i).trim();
			buf = buf.slice(i + 1);
			try {
				lines.push(JSON.parse(line));
			} catch {
				/* not a log line */
			}
		}
	});
	child.stderr!.on('data', (d: Buffer) => process.stderr.write(d));
	return { child, lines };
}

async function scenario(args: string[]) {
	const port = await freePort();
	const { child, lines } = launch(args, { PORT: String(port), HOST: '127.0.0.1', SIM_WORKERS: '2', SIM_COMMIT: 'cafe123', LOG_LEVEL: 'info' });
	const base = `http://127.0.0.1:${port}`;
	let pids: number[] = [];
	try {
		const listening = () => lines.filter((l) => l.msg === 'sim listening');
		await until('two workers listening', () => listening().length >= 2);
		pids = listening().map((l) => l.pid as number);
		assert.equal(new Set(pids).size, 2, 'two distinct worker processes');
		assert.ok(!pids.includes(child.pid!), 'the primary does not serve');

		const health = (await (await fetch(base + '/health')).json()) as Record<string, unknown>;
		assert.deepEqual(health, { status: 'ok', service: 'sim', simCommit: 'cafe123', historyInState: true, stateV: 2 });

		const res = await fetch(base + '/v1/bootstrap', {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ state: null, now: T0, player: PLAYER })
		});
		assert.equal(res.status, 200);
		assert.equal(((await res.json()) as any).snapshot.player.id, PLAYER.id);

		// a crashed worker is replaced and the service keeps answering
		process.kill(pids[0]);
		await until('respawned worker', () => listening().length >= 3);
		const res2 = await fetch(base + '/health');
		assert.equal(res2.status, 200);
	} finally {
		child.kill();
		await new Promise((r) => (child.exitCode !== null ? r(null) : child.once('exit', r)));
		// workers must not outlive the primary
		const all = lines.filter((l) => l.msg === 'sim listening').map((l) => l.pid as number);
		await until('workers gone', () => all.every((p) => !alive(p)), 10_000).catch(() => {
			for (const p of all) if (alive(p)) process.kill(p);
			throw new Error('workers outlived the primary');
		});
	}
}

describe('cluster entry point', () => {
	it('src/server.ts via tsx: 2 workers, health, bootstrap, worker respawn', async () => {
		await scenario(['--import', 'tsx', 'src/server.ts']);
	});
	it('dist/server.mjs (esbuild bundle): same', { skip: existsSync(dist) ? false : 'run `npm run build` first' }, async () => {
		await scenario(['--enable-source-maps', 'dist/server.mjs']);
	});
});
