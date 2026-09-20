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
