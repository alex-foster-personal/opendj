/**
 * Source pins for the process-family snapshot module (PERF-UI-6785).
 *
 * Every test that reads a response body lives in
 * process-family-snapshot-fetch.test.mjs, where the body comes from the real
 * GET /api/v1/performance/telemetry/processes route (AGENTS.md: controls come
 * from the production endpoint or a verified capture, never hand-built). This
 * file keeps only what cannot be driven by a body: the monotonic-clock rule.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// PERF-UI-6785 (Codex review on #6785): every age and interval in the snapshot
// module is monotonic. SOURCE PIN, because the behavior it guards (a wall
// clock moved BACK after a fetch) cannot be produced in a unit test without
// faking Date: this host has no faketime-style clock to inject into a real
// subprocess, and moving the real system clock would disturb every other
// process on the machine. The behavior tests in
// process-family-snapshot-fetch.test.mjs drive the same functions with
// explicit monotonic times.
test('the snapshot module measures ages on the monotonic clock, never Date', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/process-family-snapshot.ts', import.meta.url)),
		'utf8'
	);
	const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
	assert.doesNotMatch(code, /\bDate\b/, 'no wall-clock read or delta in code');
	assert.match(code, /performance\.now\(\)/, 'the monotonic clock is the one it reads');
	for (const fn of [
		'readFreshProcessFamilySnapshot',
		'processFamilyRefreshDue',
		'readUnavailableProbeStale',
		'processFamilySnapshotAgeMs'
	]) {
		assert.match(
			code,
			new RegExp(`export function ${fn}\\([^)]*monoNowMs: number = _monoNowMs\\(\\)`),
			fn
		);
	}
	const meters = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/PerfMeters.svelte', import.meta.url)),
		'utf8'
	);
	assert.doesNotMatch(
		meters,
		/(readFreshProcessFamilySnapshot|processFamilyRefreshDue|readUnavailableProbeStale|processFamilySnapshotAgeMs)\(Date\.now\(\)/
	);
});
