// Bundles the service (src/server.ts + the vendored lib it imports via $lib) into dist/server.mjs.
// The runtime image then needs no node_modules and no vendor sources.
import { build } from 'esbuild';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const libDir = path.resolve(root, '../vendor/frontend/web/src/lib');
rmSync(path.join(root, 'dist'), { recursive: true, force: true });
mkdirSync(path.join(root, 'dist'), { recursive: true });

const result = await build({
	entryPoints: [path.join(root, 'src/server.ts')],
	outfile: path.join(root, 'dist/server.mjs'),
	bundle: true,
	platform: 'node',
	format: 'esm',
	target: 'node20',
	sourcemap: 'linked',
	legalComments: 'none',
	metafile: true,
	logLevel: 'info',
	tsconfig: path.join(root, 'tsconfig.json')
});

// Guard: the server import graph must stay free of browser-only code (rendering, scenes, UI, audio, three, svelte).
const inputs = Object.keys(result.metafile.inputs).map((p) => path.resolve(root, p));
const lib = inputs.filter((p) => p.startsWith(libDir + path.sep)).map((p) => path.relative(libDir, p).split(path.sep).join('/'));
const allowed = /^(api|content|sim|game)\//;
const offenders = inputs.filter((p) => /node_modules|three|svelte/i.test(path.relative(root, p))).concat(lib.filter((p) => !allowed.test(p)));
if (offenders.length) {
	console.error('browser-only / unexpected modules reached the sim bundle:\n  ' + offenders.join('\n  '));
	process.exit(1);
}
writeFileSync(path.join(root, 'dist/lib-modules.txt'), lib.sort().join('\n') + '\n');
console.log(`bundle ok: ${lib.length} vendored lib modules (see dist/lib-modules.txt)`);
