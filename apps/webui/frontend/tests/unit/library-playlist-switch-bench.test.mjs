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
