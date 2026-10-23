import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

/**
 * Commit of the vendored frontend whose lib/sim this process runs: env SIM_COMMIT (set by the Docker build arg),
 * else `git -C ../vendor/frontend rev-parse --short HEAD`, else "unknown". Resolved once at start.
 */
export function resolveSimCommit(): string {
	const fromEnv = process.env.SIM_COMMIT?.trim();
	if (fromEnv) return fromEnv;
	try {
		const vendor = fileURLToPath(new URL('../../vendor/frontend', import.meta.url));
		const out = execFileSync('git', ['-C', vendor, 'rev-parse', '--short', 'HEAD'], {
			encoding: 'utf8',
			stdio: ['ignore', 'pipe', 'ignore'],
			timeout: 5000
		});
		return out.trim() || 'unknown';
	} catch {
		return 'unknown';
	}
}
