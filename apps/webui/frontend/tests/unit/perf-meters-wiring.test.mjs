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
