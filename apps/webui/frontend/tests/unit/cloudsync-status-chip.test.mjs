import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const CHIP = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/CloudSyncStatusChip.svelte', import.meta.url)),
	'utf8'
);
const LAYOUT = readFileSync(
	fileURLToPath(new URL('../../src/routes/+layout.svelte', import.meta.url)),
	'utf8'
);
const TOPBAR = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);

test('the status chip has off, syncing, ok and error render states', () => {
	for (const state of ['off', 'syncing', 'ok', 'error']) {
		assert.match(CHIP, new RegExp(`return '${state}'`), `missing ${state} state`);
	}
});

test('the chip carries the complete status object in its hover title', () => {
	assert.match(CHIP, /const title = \$derived\.by\(/);
	assert.match(CHIP, /title=\{title\}/);
	assert.match(CHIP, /Click to open recent results\./);
});

test('clicking the chip opens the five-result detail instead of a dead badge', () => {
	assert.match(CHIP, /onclick=\{\(\) => \(detailsOpen = !detailsOpen\)\}/);
	assert.match(CHIP, /status\.recent_results/);
	assert.match(CHIP, /No sync attempts have completed yet\./);
});

test('the chip is rendered immediately beside the account bauble', () => {
	const chipAt = LAYOUT.indexOf('<CloudSyncStatusChip />');
	const baubleAt = LAYOUT.indexOf('<UserBauble />');
	assert.ok(chipAt >= 0, 'the app shell must render the CloudSync status chip');
	assert.ok(baubleAt > chipAt, 'the CloudSync chip must be beside and before the account bauble');
	assert.ok(TOPBAR.indexOf('<CloudSyncStatusChip />') < TOPBAR.indexOf('<UserBauble size={20} />'));
});
