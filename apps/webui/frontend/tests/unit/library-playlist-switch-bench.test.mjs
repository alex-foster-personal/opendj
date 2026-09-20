// requirement: PERF-UI-05
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { THRESHOLDS_P50_MS } from '../e2e/support/playlist-switch-bench-stats.mjs';

const fixturePath = fileURLToPath(
	new URL('../fixtures/library-playlist-switch-bench.json', import.meta.url)
);

test('bench thresholds are shared between stats helper and fixture', () => {
	const payload = JSON.parse(readFileSync(fixturePath, 'utf8'));
	assert.deepEqual(payload.thresholds_p50_ms, THRESHOLDS_P50_MS);
});

test('bench fixture records live-measured post_fix when present', () => {
	const payload = JSON.parse(readFileSync(fixturePath, 'utf8'));
	if (payload.sha !== 'live-bench') {
		return;
	}
	for (const [metric, cap] of Object.entries(THRESHOLDS_P50_MS)) {
		const measured = payload.post_fix[metric]?.p50;
		assert.ok(
			typeof measured === 'number' && measured <= cap,
			`${metric} post-fix p50 ${measured} must be <= ${cap}`
		);
	}
});

// A .d.mts declaration file can drift from the .mjs it describes and nothing
// notices: `svelte-check` reads the declaration, `node --test` reads the
// module, and neither compares them. `playlist-switch-bench-stats.d.mts`
// exists because this module has to stay plain JS for this runner while
// library-playlist-switch-latency.spec.ts imports it from TypeScript under
// `allowJs: false`. This is the comparison nobody else makes.
//
//   if an export named in the declaration is missing or the wrong kind here
//     then broken (svelte-check would keep passing against a lie)
test('the stats module exports exactly what its declaration file promises', async () => {
	const mod = await import('../e2e/support/playlist-switch-bench-stats.mjs');
	const declaration = readFileSync(
		fileURLToPath(new URL('../e2e/support/playlist-switch-bench-stats.d.mts', import.meta.url)),
		'utf8'
	);

	const declaredFunctions = [...declaration.matchAll(/^export function (\w+)/gm)].map((m) => m[1]);
	const declaredConsts = [...declaration.matchAll(/^export const (\w+)/gm)].map((m) => m[1]);
	assert.ok(
		declaredFunctions.length > 0 && declaredConsts.length > 0,
		'the declaration must actually declare something, or this test asserts nothing'
	);

	for (const name of declaredFunctions) {
		assert.equal(typeof mod[name], 'function', `${name} is declared as a function`);
	}
	for (const name of declaredConsts) {
		assert.notEqual(mod[name], undefined, `${name} is declared as a const`);
	}
	assert.deepEqual(
		Object.keys(mod).sort(),
		[...declaredFunctions, ...declaredConsts].sort(),
		'the declaration must name every export and no others'
	);

	// The one const carries a shape the spec indexes by key, so pin its keys too.
	const declaredKeys = [
		...declaration
			.slice(declaration.indexOf('export const THRESHOLDS_P50_MS'))
			.matchAll(/readonly (\w+):/g)
	].map((m) => m[1]);
	assert.deepEqual(Object.keys(mod.THRESHOLDS_P50_MS).sort(), declaredKeys.sort());
});
