// requirement: LIBPG-7694d
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '../../src/lib/components/rb/browser');
const source =
	readFileSync(join(root, 'PlaylistSetTabs.svelte'), 'utf8') +
	readFileSync(join(root, 'PlaylistSetTabsView.svelte'), 'utf8');

test('LIBPG-7694d set start buttons show the run status', () => {
	assert.match(source, /data-testid="set-run-status"/);
	assert.match(source, /Starting \$\{kind\}\.\.\./);
	assert.match(source, /\$\{name\} running\. Play count/);
	assert.match(source, /Could not start \$\{kind\}/);
	assert.match(source, /aria-pressed=\{runIsLive\('practice'\)\}/);
	assert.match(source, /aria-pressed=\{runIsLive\('performance'\)\}/);
});

test('LIBPG-7694d a failed run rolls the optimistic play count back', () => {
	assert.match(source, /patchPlayCount\(setId, previous \+ 1\)/);
	assert.match(source, /catch \(error\) \{\s*patchPlayCount\(setId, previous\)/);
});

test('LIBPG-7694d the run status line explains its play count on hover', () => {
	assert.match(source, /data-testid="set-run-status"\s*title="[^"]*play count/);
});
