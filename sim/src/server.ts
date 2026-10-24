// Entry point. Primary process forks SIM_WORKERS (default 2, explicit 0..256) workers that share the listening port;
// SIM_WORKERS=0 runs a single in-process server (debugging, tests).

import cluster from 'node:cluster';
import { createSimServer, makeLogger } from './app';
import { workerCount } from './config';
import { resolveSimCommit } from './commit';

const log = makeLogger();
const port = Number(process.env.PORT ?? 8080);
const host = process.env.HOST ?? '0.0.0.0';

function listen(simCommit: string) {
	const server = createSimServer({ simCommit, log });
	server.listen(port, host, () => log({ level: 'info', msg: 'sim listening', host, port, simCommit }));
	const stop = () => {
		server.close(() => process.exit(0));
		setTimeout(() => process.exit(0), 5000).unref();
	};
	process.on('SIGTERM', stop);
	process.on('SIGINT', stop);
}

function primary(workers: number) {
	const simCommit = resolveSimCommit();
	process.env.SIM_COMMIT = simCommit; // inherited by the workers: one git lookup per container
	log({ level: 'info', msg: 'sim primary starting', workers, simCommit, node: process.version });

	let stopping = false;
	const recentExits: number[] = [];
	for (let i = 0; i < workers; i++) cluster.fork();

	cluster.on('exit', (w, code, signal) => {
		if (stopping) {
			if (Object.keys(cluster.workers ?? {}).length === 0) process.exit(0);
			return;
		}
		log({ level: 'error', msg: 'worker exited, respawning', worker: w.process.pid, code, signal });
		const now = Date.now();
		recentExits.push(now);
		while (recentExits.length && now - recentExits[0] > 10_000) recentExits.shift();
		if (recentExits.length > 10) {
			log({ level: 'error', msg: 'workers keep crashing, giving up' });
			process.exit(1);
		}
		setTimeout(() => cluster.fork(), 300);
	});

	const stop = () => {
		if (stopping) return;
		stopping = true;
		for (const w of Object.values(cluster.workers ?? {})) w?.process.kill('SIGTERM');
		if (Object.keys(cluster.workers ?? {}).length === 0) process.exit(0);
		setTimeout(() => process.exit(0), 6000).unref();
	};
	process.on('SIGTERM', stop);
	process.on('SIGINT', stop);
}

const workers = workerCount();
if (workers === 0) listen(resolveSimCommit());
else if (cluster.isPrimary) primary(workers);
else listen(process.env.SIM_COMMIT ?? 'unknown');
