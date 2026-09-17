/**
 * Caps registry: every capped cache re-evicts from one place.
 *
 * Regression lines:
 * - if applyAllCaps skips a registered cache then that cache ignores tier, posture and pressure changes - broken
 * - if a Vite HMR re-registration duplicates a consumer then eviction runs twice and stale closures survive - broken
 * - if one throwing cache stops the rest then a bug in one cache leaves every other cache over budget - broken
 * - if a stale unregister handle removes the newer registration then HMR silently unwires a cache - broken
 * - if a capped cache stops registering, or a call site goes back to calling one cache's apply by hand, then the next cache added can miss a cap change again - broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => readFileSync(join(FRONTEND, rel), 'utf8');

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/cache-caps-registry.ts');
});

test('applyAllCaps runs every registered consumer', () => {
	const calls = [];
	const offA = mod.registerCapsConsumer('test-a', () => calls.push('a'));
	const offB = mod.registerCapsConsumer('test-b', () => calls.push('b'));
	mod.applyAllCaps();
	assert.deepEqual(
		calls.sort(),
		['a', 'b'],
		'if applyAllCaps skips a registered cache then that cache ignores tier, posture and pressure changes - broken'
	);
	offA();
	offB();
});

test('re-registering a name replaces it, and a stale handle cannot remove the newer one', () => {
	const calls = [];
	const staleOff = mod.registerCapsConsumer('test-hmr', () => calls.push('old'));
	const off = mod.registerCapsConsumer('test-hmr', () => calls.push('new'));
	mod.applyAllCaps();
	assert.deepEqual(
		calls,
		['new'],
		'if a Vite HMR re-registration duplicates a consumer then eviction runs twice and stale closures survive - broken'
	);
	staleOff();
	calls.length = 0;
	mod.applyAllCaps();
	assert.deepEqual(
		calls,
		['new'],
		'if a stale unregister handle removes the newer registration then HMR silently unwires a cache - broken'
	);
	off();
	calls.length = 0;
	mod.applyAllCaps();
	assert.deepEqual(calls, [], 'if the live handle cannot unregister then a disposed cache is still evicted - broken');
});

test('one throwing consumer does not stop the others, and the failure still surfaces by name', () => {
	const calls = [];
	const offBad = mod.registerCapsConsumer('test-bad', () => {
		throw new Error('boom');
	});
	const offGood = mod.registerCapsConsumer('test-good', () => calls.push('good'));
	assert.throws(
		() => mod.applyAllCaps(),
		(error) => error instanceof AggregateError && /test-bad/.test(error.errors[0].message)
	);
	assert.deepEqual(
		calls,
		['good'],
		'if one throwing cache stops the rest then a bug in one cache leaves every other cache over budget - broken'
	);
	offBad();
	offGood();
});

const CAPPED_CACHES = {
	'src/lib/rb/audio-prefetch-cache.svelte.ts': 'audio-prefetch',
	'src/lib/player/preview-cue.svelte.ts': 'preview-pcm',
	'src/lib/components/rb/wave/anlz-cache.svelte.ts': 'anlz'
};
const CAP_CHANGE_SITES = [
	'src/lib/rb/perf-tier-client.ts',
	'src/lib/rb/app-posture-prefs.ts',
	'src/lib/rb/prefs-hydrate.ts',
	'src/lib/rb/app-init.ts'
];

test('every capped cache registers, and every cap-change site calls applyAllCaps rather than one cache', () => {
	for (const [file, name] of Object.entries(CAPPED_CACHES)) {
		assert.match(
			read(file),
			new RegExp(`registerCapsConsumer\\('${name}',`),
			`if ${file} stops registering then that cache ignores cap changes - broken`
		);
	}
	for (const file of CAP_CHANGE_SITES) {
		const source = read(file);
		assert.match(source, /applyAllCaps/, `if ${file} does not call applyAllCaps then a cap change skips every cache - broken`);
		assert.doesNotMatch(
			source,
			/apply(Prefetch|Preview|Anlz)Caps\(/,
			`if a call site goes back to calling one cache's apply by hand then the next cache added can miss a cap change again - broken (${file})`
		);
	}
});
