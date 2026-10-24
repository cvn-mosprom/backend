import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { buildPolicy } from './generate-csp.mjs';

test('every prerendered page bootstrap is allowed and external script text is excluded', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mosprom-csp-'));
  try {
    const one = '\nwindow.bootstrap = true;\n';
    const two = 'window.landing = true;';
    await writeFile(join(root, 'index.html'), `<script>${one}</script><script src="https://untrusted.test/x">not allowed</script>`);
    await mkdir(join(root, 'nested'));
    await writeFile(join(root, 'nested/privacy.html'), `<script type="module">${two}</script>`);
    const policy = await buildPolicy(root);
    for (const value of [one, two]) assert.ok(policy.includes(`'sha256-${createHash('sha256').update(value).digest('base64')}'`));
    const scripts = policy.split(';').find(part => part.includes('script-src'));
    assert.ok(!scripts.includes('unsafe-inline'));
    assert.ok(!policy.includes('untrusted.test'));
    assert.ok(!policy.includes(createHash('sha256').update('not allowed').digest('base64')));
    assert.ok(policy.includes('https://*.{$ROUTING_ZONE}'));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('a missing bootstrap fails the build instead of silently weakening CSP', async () => {
  const root = await mkdtemp(join(tmpdir(), 'mosprom-csp-'));
  try {
    await writeFile(join(root, 'index.html'), '<script src="app.js"></script>');
    await assert.rejects(buildPolicy(root), /No inline bootstrap/);
  } finally { await rm(root, { recursive: true, force: true }); }
});
