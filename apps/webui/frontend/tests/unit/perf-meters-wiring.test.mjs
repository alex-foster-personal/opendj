/**
 * PERFMODE-05 (issue #1987): PerfMeters reset paths and sampler wiring.
 *
 * Regression: ANLZ/prefetch/perf-ring resets call the correct APIs;
 * sampler uses perfMeterSampleIntervalMs with untrack, not a raw 2000ms timer.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

test('PerfMeters wires the three reset paths', () => {
	const meters = source('src/lib/components/rb/PerfMeters.svelte');
	assert.match(meters, /invalidateAllAnlzCacheEntries/);
	assert.match(meters, /clearAudioPrefetchCache/);
	assert.match(meters, /resetPerfEventLog/);
});

test('resetPerfEventLog calls flushPerfEventLog', () => {
	const perfLog = source('src/lib/rb/perf-event-log.ts');
	assert.match(perfLog, /export function resetPerfEventLog/);
	assert.match(
		perfLog,
		/export function resetPerfEventLog[\s\S]*flushPerfEventLog\(\)/,
		'reset must persist the empty ring'
	);
});

test('PerfMeters samples with untrack and perfMeterSampleIntervalMs', () => {
	const meters = source('src/lib/components/rb/PerfMeters.svelte');
	assert.match(meters, /untrack/);
	assert.match(meters, /perfMeterSampleIntervalMs/);
	assert.doesNotMatch(
		meters,
		/setInterval\(\(\) => untrack\(_updateMemory\), 2000\)/,
		'the old fixed 2000ms interval must not return'
	);
});

test('PerfMeters open panel points at Chrome Task Manager capture', () => {
	const meters = source('src/lib/components/rb/PerfMeters.svelte');
	assert.match(meters, /Chrome Task Manager/);
	assert.match(meters, /Shift\+Esc/);
});

test('the memory figure counts the engine processes and the closed bar refreshes them (PERF-UI-6785)', () => {
	const meters = source('src/lib/components/rb/PerfMeters.svelte');
	// regression: the figure was the page alone because only the open monitor fetched the walk
	assert.match(meters, /processes: processSnapshot\?\.members \?\? null/);
	// a stale native-probe sample the engine left out is named in the hover, never summed
	assert.match(meters, /probeStale: processSnapshot\s*\?\s*processSnapshot\.probeStale/);
	// and from an unavailable response (no live walk, stale probe sample) too
	assert.match(meters, /: readUnavailableProbeStale\(PROCESS_SNAPSHOT_MAX_AGE_MS\)/);
	assert.match(
		meters,
		/const tick = \(\): void => \{\s*untrack\(_sampleTick\);\s*_refreshProcesses\(false\);/,
		'the closed bar must refresh the process walk, or the figure never counts the engine'
	);
	assert.match(meters, /PROCESS_SNAPSHOT_MAX_AGE_MS/, 'a stale walk must not be shown as current');
	assert.match(meters, /memoryMB: memoryTotalMB/, 'the chart plots the same figure the meter shows');
});

test('every reader that shows the processes as current goes through the expiry rule (PERF-UI-6785)', () => {
	const meters = source('src/lib/components/rb/PerfMeters.svelte');
	// The raw cache outlives a failed fetch, so reading it shows aged-out
	// processes as current: the pinned breakdown did, while the figure did not.
	assert.doesNotMatch(meters, /readProcessFamilySnapshot\(/, 'no reader bypasses the expiry');
	assert.match(meters, /readFreshProcessFamilySnapshot\(PROCESS_SNAPSHOT_MAX_AGE_MS\)/);
	const breakdown = /const breakdown = \$derived\([\s\S]*?\n\t\);/.exec(meters)?.[0] ?? '';
	assert.match(breakdown, /processes: breakdownProcesses/, 'the breakdown lists the expiry-checked snapshot');
	assert.match(breakdown, /kernelLevel: breakdownProcesses\?\.kernelLevel/);
	assert.match(breakdown, /churnScore: breakdownProcesses\?\.churnScore/);
	const assignments = meters.match(/(?<!let )breakdownProcesses = [^;]+;/g) ?? [];
	assert.ok(assignments.length >= 2, 'refreshed on every tick and every fetch');
	assert.ok(assignments.every((a) => a === 'breakdownProcesses = _freshProcessSnapshot();'), assignments.join(' | '));
});
