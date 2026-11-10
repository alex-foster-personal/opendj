/**
 * PERF-GRID-12026: the deck banner's presentation-clock failure is a
 * reactive cell, so `Deck.svelte`'s `controlError` reads only that value.
 *
 * It used to read it through `queryPerformanceState()`, which built the whole
 * app snapshot: every deck's derived then depended on every signal and re-ran
 * on every bus command (about 8 ms per command per run, Tue 10 Nov). The broad
 * dependency was also what kept the banner updating, because the value lived
 * in a plain module variable. This file runs the real Svelte runtime
 * (load-rune-module.mjs); the plain TypeScript loader stubs `$state` away and
 * cannot see reactivity.
 *
 * [if] a derived over the failure does not follow a note and a clear [then]
 *   broken (the banner would freeze once the snapshot dependency is gone)
 * [if] a per-frame clear with nothing standing writes the signal [then] broken
 *   (the derived re-runs every frame, or throws state_unsafe_mutation)
 * [if] Deck.svelte's controlError calls queryPerformanceState again [then]
 *   broken (every bus command re-runs four deck deriveds)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"import { clearPresentationTickFailure, notePresentationTickFailure } from '$lib/rb/presentation-clock-report';",
	"import { presentationTickFailure } from '$lib/rb/presentation-tick-failure.svelte';",
	"import { flushSync } from 'svelte';",
	'export { clearPresentationTickFailure, notePresentationTickFailure, flushSync };',
	'export function watchBanner(): { seen: (string | null)[]; runs: () => number; stop: () => void } {',
	'	const seen: (string | null)[] = [];',
	'	let derivedRuns = 0;',
	'	const stop = $effect.root(() => {',
	'		const bannerText = $derived.by(() => {',
	'			derivedRuns += 1;',
	'			return presentationTickFailure() ? `Audio presentation clock failed: ${presentationTickFailure()}` : null;',
	'		});',
	'		$effect(() => {',
	'			seen.push(bannerText);',
	'		});',
	'	});',
	'	flushSync();',
	'	return { seen, runs: () => derivedRuns, stop };',
	'}'
].join('\n');

test('the banner text follows a note and a clear of the presentation tick failure', async () => {
	const mod = await loadRuneModule(ENTRY);
	mod.clearPresentationTickFailure();
	const { seen, runs, stop } = mod.watchBanner();
	assert.deepEqual(seen, [null], 'the banner did not start clear');

	mod.notePresentationTickFailure(new Error('timestamp device disappeared'));
	mod.flushSync();
	assert.deepEqual(
		seen,
		[null, 'Audio presentation clock failed: timestamp device disappeared'],
		'a noted failure did not reach the banner derived, so the cell is not reactive'
	);

	mod.clearPresentationTickFailure();
	mod.flushSync();
	assert.deepEqual(seen.at(-1), null, 'a clear did not reach the banner derived');

	const before = runs();
	for (let frame = 0; frame < 5; frame += 1) mod.clearPresentationTickFailure();
	mod.flushSync();
	assert.equal(runs(), before, 'a per-frame clear with nothing standing re-ran the derived');
	stop();
});

test('Deck.svelte controlError reads the failure cell, not the whole performance snapshot', () => {
	const source = readFileSync(
		new URL('../../src/lib/components/rb/Deck.svelte', import.meta.url),
		'utf8'
	);
	const derived = source.match(/const controlError[^;]*\$derived\(([\s\S]*?)\n\t\);/);
	assert.ok(derived, 'controlError derived not found in Deck.svelte');
	assert.doesNotMatch(
		derived[1],
		/queryPerformanceState/,
		'controlError builds the whole snapshot again, so every bus command re-runs it'
	);
	assert.match(
		derived[1],
		/presentationTickFailure\(\)/,
		'controlError no longer reads the failure cell'
	);
});
