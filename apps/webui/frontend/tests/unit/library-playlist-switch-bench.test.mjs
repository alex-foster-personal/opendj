// requirement: PERF-UI-05
import assert from 'node:assert/strict';
import { execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { THRESHOLDS_P50_MS } from '../e2e/support/playlist-switch-bench-stats.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const fixturePath = fileURLToPath(
	new URL('../fixtures/library-playlist-switch-bench.json', import.meta.url)
);
const COMMITTED_FIXTURE_PATH = join(
	FRONTEND_ROOT,
	'tests',
	'fixtures',
	'library-playlist-switch-bench.json'
);
const DEFAULT_OUT_PATH = join(
	FRONTEND_ROOT,
	'test-results',
	'library-playlist-switch-bench.json'
);
const isRegressionChild = process.env.PLAYLIST_SWITCH_BENCH_REGRESSION_CHILD === '1';

test('bench thresholds are shared between stats helper and fixture', () => {
	const payload = JSON.parse(readFileSync(fixturePath, 'utf8'));
	assert.deepEqual(payload.thresholds_p50_ms, THRESHOLDS_P50_MS);
});

test('e2e default output path is not the committed fixture', () => {
	const specSource = readFileSync(
		fileURLToPath(
			new URL('../e2e/library-playlist-switch-latency.spec.ts', import.meta.url)
		),
		'utf8'
	);
	assert.match(specSource, /DEFAULT_OUT_PATH = join\([\s\S]*?'test-results'/);
	assert.match(
		specSource,
		/OUT_PATH = resolve\(process\.env\.PLAYLIST_SWITCH_BENCH_OUT \?\? DEFAULT_OUT_PATH\)/
	);
	assert.notEqual(resolve(DEFAULT_OUT_PATH), resolve(COMMITTED_FIXTURE_PATH));
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

if (!isRegressionChild) {
	test('unit gate unchanged after simulated e2e fixture overwrite', () => {
		const original = readFileSync(fixturePath, 'utf8');
		const poisoned = {
			...JSON.parse(original),
			sha: 'live-bench',
			post_fix: {
				playlist_tree_ready_ms: { p50: 99999, p95: 99999 },
				playlist_switch_first_rows_ms: { p50: 99999, p95: 99999 },
				all_tracks_first_rows_ms: { p50: 99999, p95: 99999 }
			}
		};
		writeFileSync(fixturePath, `${JSON.stringify(poisoned, null, 2)}\n`);
		try {
			execSync('node --test tests/unit/library-playlist-switch-bench.test.mjs', {
				cwd: FRONTEND_ROOT,
				stdio: 'pipe',
				env: {
					...process.env,
					PLAYLIST_SWITCH_BENCH_REGRESSION_CHILD: '1'
				}
			});
		} finally {
			writeFileSync(fixturePath, original);
		}
	});
}
