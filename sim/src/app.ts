// Server factory (also used by the tests): one http.Server around the request handler.

import { createServer, type Server } from 'node:http';
import { createRequestHandler, type HandlerOptions } from './http';

const LEVELS: Record<string, number> = { debug: 10, info: 20, warn: 30, error: 40 };

export function makeLogger(level = process.env.LOG_LEVEL ?? 'info'): NonNullable<HandlerOptions['log']> {
	const min = LEVELS[level.toLowerCase()] ?? LEVELS.info;
	return (line) => {
		if ((LEVELS[String(line.level)] ?? LEVELS.info) < min) return;
		const out = JSON.stringify({ ts: new Date().toISOString(), service: 'sim', pid: process.pid, ...line });
		(line.level === 'error' ? process.stderr : process.stdout).write(out + '\n');
	};
}

export function createSimServer(opts: HandlerOptions): Server {
	const handler = createRequestHandler(opts);
	const server = createServer((req, res) => {
		void handler(req, res);
	});
	server.requestTimeout = 30_000;
	server.headersTimeout = 15_000;
	server.keepAliveTimeout = 65_000;
	return server;
}
