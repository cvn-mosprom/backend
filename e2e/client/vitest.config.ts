import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// The REAL frontend client (vendor/frontend/web/src/lib/api/http/HttpGameApi.ts) is imported as source; `$lib/*` is the SvelteKit alias.
const lib = fileURLToPath(new URL('../../vendor/frontend/web/src/lib', import.meta.url));
const base = (process.env.E2E_BASE ?? 'https://localhost').replace(/\/+$/, '');

export default defineConfig({
	resolve: { alias: { $lib: lib } },
	// vendor/frontend/web/tsconfig.json extends a SvelteKit-generated file that does not exist here: do not look it up
	esbuild: { tsconfigRaw: JSON.stringify({ compilerOptions: { target: 'ES2022', useDefineForClassFields: false } }) },
	test: {
		include: ['scenarios/**/*.test.ts', '../../vendor/frontend/web/src/lib/hr/*.live.test.ts'],
		environment: 'node',
		setupFiles: ['./scenarios/setup.ts'],
		// the app reads VITE_API_URL (web/.env); here it is the e2e stack (Caddy, self-signed cert, see setup.ts)
		env: { VITE_API_URL: base },
		testTimeout: 180_000,
		hookTimeout: 60_000
	}
});
