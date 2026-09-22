/**
 * The rune harness bundles each entry source ONCE per process and still hands
 * every caller a fresh module instance.
 *
 * Both halves are load-bearing. Without the memo, a file such as
 * autoplay-stall-persistence bundled its one entry once per test (19 times)
 * and sat at the runner's 60 s per-file timeout on loaded agentbox hosts
 * (Tue 22 Sep 2026, jobs 106269849983, 106613353127, 106743215758) with every
 * test passing. Without fresh instances, the memo would leak one test's rune
 * state into the next, which is the over-correction this file exists to catch.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule, runeBundleCount } from './load-rune-module.mjs';

const ENTRY = [
	'export const counter = $state({ n: 0 });',
	'export function bump() {',
	'\tcounter.n += 1;',
	'}'
].join('\n');

test('the same entry source is bundled once and evaluated fresh per load', async () => {
	const before = runeBundleCount();
	const first = await loadRuneModule(ENTRY);
	assert.equal(runeBundleCount(), before + 1, 'the first load bundles the entry');

	const second = await loadRuneModule(ENTRY);
	assert.equal(runeBundleCount(), before + 1, 'the second load reuses the bundle');
	assert.notEqual(first, second, 'each load is a distinct module instance');

	first.bump();
	assert.equal(first.counter.n, 1);
	assert.equal(second.counter.n, 0, 'rune state never leaks between instances');
});

test('a different entry source is bundled on its own', async () => {
	const before = runeBundleCount();
	const other = await loadRuneModule(`${ENTRY}\nexport const tag = 'other';`);
	assert.equal(runeBundleCount(), before + 1);
	assert.equal(other.tag, 'other');
});
