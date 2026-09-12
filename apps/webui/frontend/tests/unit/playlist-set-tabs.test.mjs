import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '../../src/lib/components/rb/browser');
const source = readFileSync(join(root, 'PlaylistSetTabs.svelte'), 'utf8');

test('SET-05 PlaylistSetTabs refetches when playlistId changes', () => {
	assert.match(source, /\$effect\(\(\) => \{[\s\S]*const id = playlistId/);
	assert.match(source, /selectedId = null/);
});

test('SET-05 PlaylistSetTabs exposes practice and perform controls', () => {
	assert.match(source, /data-testid="playlist-set-tab"/);
	assert.match(source, /data-testid="set-mode-practice"/);
	assert.match(source, /data-testid="set-mode-perform"/);
	assert.match(source, /data-testid="set-run-practice"/);
	assert.match(source, /data-testid="set-run-perform"/);
	assert.match(source, /\n\s+Practice\n/);
	assert.match(source, /\n\s+Perform\n/);
});
