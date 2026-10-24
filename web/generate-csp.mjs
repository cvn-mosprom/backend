/** Build an exact allowlist for SvelteKit's inline bootstrap, without unsafe-inline. */
import { createHash } from 'node:crypto';
import { readdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

export async function buildPolicy(directory) {
  const hashes = new Set();
  async function visit(folder) {
    for (const entry of await readdir(folder, { withFileTypes: true })) {
      const path = join(folder, entry.name);
      if (entry.isDirectory()) await visit(path);
      else if (entry.name.endsWith('.html')) {
        const html = await readFile(path, 'utf8');
        for (const script of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
          if (/\bsrc\s*=/i.test(script[1]) || !script[2].trim()) continue;
          hashes.add(`'sha256-${createHash('sha256').update(script[2]).digest('base64')}'`);
        }
      }
    }
  }
  await visit(directory);
  if (!hashes.size) throw new Error('No inline bootstrap found: review the web CSP build contract');
  return [
    "default-src 'self'",
    `script-src 'self' https://telegram.org 'wasm-unsafe-eval' ${[...hashes].sort().join(' ')}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https://*.telegram.org https://*.t.me",
    "media-src 'self' blob:", "font-src 'self'",
    "connect-src 'self' https://*.{$ROUTING_ZONE}", "worker-src 'self' blob:",
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org", "base-uri 'self'", "object-src 'none'"
  ].join('; ');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv.length !== 4) throw new Error('Usage: node generate-csp.mjs <build directory> <output.caddy>');
  const policy = await buildPolicy(process.argv[2]);
  await writeFile(process.argv[3], `header Content-Security-Policy ${JSON.stringify(policy)}\n`);
}
