/**
 * OSSPUB-04: the release stage is shown beside the wordmark.
 *
 * - if the stage constant is not "alpha" while the product is alpha, or its
 *   title does not tell the user to keep a backup, then broken
 * - if the badge hard-codes its word instead of reading the constant, then a
 *   move to beta leaves a stale "alpha" on screen: broken
 * - if the badge is not mounted directly after the "open dj" wordmark, then
 *   the stage reads as belonging to something else: broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const read = (rel) => readFileSync(`${SRC}/${rel}`, 'utf8');

let stage;

before(async () => {
	stage = await loadTypeScriptModule('src/lib/release-stage.ts');
});

test('OSSPUB-04 if the stage is not alpha or its title omits the backup advice then broken', () => {
	assert.equal(stage.RELEASE_STAGE, 'alpha');
	assert.match(stage.RELEASE_STAGE_TITLE, /alpha software/i);
	assert.match(stage.RELEASE_STAGE_TITLE, /backup/i);
});

test('OSSPUB-04 if the badge hard-codes its word instead of reading the constant then broken', () => {
	const badge = read('lib/components/AlphaBadge.svelte');
	assert.match(badge, /\{RELEASE_STAGE\}/);
	assert.match(badge, /title=\{RELEASE_STAGE_TITLE\}/);
	assert.doesNotMatch(badge.replace(/<style>[\s\S]*<\/style>/, ''), />\s*alpha\s*</i);
});

test('OSSPUB-04 if the badge is not mounted directly after the wordmark then broken', () => {
	const panel = read('lib/components/rb/BrowserPanel.svelte');
	assert.match(panel, /<span class="wordmark">open dj<\/span>\s*<AlphaBadge \/>/);
	assert.match(panel, /import AlphaBadge from '\$lib\/components\/AlphaBadge\.svelte';/);
});

test('OSSPUB-04 if the first-run screen shows the name without the badge then broken', () => {
	const screen = read('lib/components/preflight/PreflightScreen.svelte');
	assert.match(screen, /<span class="preflight-name">Open DJ<AlphaBadge \/><\/span>/);
	assert.match(screen, /import AlphaBadge from '\$lib\/components\/AlphaBadge\.svelte';/);
});
