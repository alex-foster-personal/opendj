/**
 * NATIVE-5886: the engine runs next-up analysis while a deck plays only when
 * the page mirror carries an xrun counter (apps/webui/server/ahead_analysis.py
 * `mirror_xruns`). Without one it holds that work. So the page must publish
 * the counter on every mirror document, from the first one, or the feature is
 * off everywhere.
 *
 * Source-shape assertions for the mirror: buildUiMirror reads the live audio
 * engine, which node:test cannot construct. The counter value itself is
 * computed here with the real fold.
 *
 * Regression lines:
 *   - if the mirror document has no xrun_sentinel then broken
 *   - if the counter is published only once the sentinel is armed then broken
 *   - if the mirror publishes the count without the detector's state then the engine holds while-playing work
 *   - if xruns is ever not a non-negative integer then the engine refuses the mirror field
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { EMPTY_XRUN_SESSION, foldXrunReport } from '../../src/lib/rb/xrun-math.ts';

function source(name) {
	return readFileSync(fileURLToPath(new URL(`../../src/lib/rb/${name}`, import.meta.url)), 'utf8');
}

const MIRROR = source('ui-mirror.ts');
const SENTINEL = source('xrun-sentinel.ts');

test('every mirror document carries the session xrun counter', () => {
	const build = MIRROR.slice(
		MIRROR.indexOf('export function buildUiMirror'),
		MIRROR.indexOf('export function installUiMirror')
	);
	assert.ok(build.length > 0, 'control: buildUiMirror was found');
	assert.match(build, /^\t\txrun_sentinel: readXrunSentinelMirror\(\),$/m);
	assert.match(MIRROR, /^import \{ readXrunSentinelMirror \} from '\.\/xrun-sentinel';$/m);
	const read = SENTINEL.slice(
		SENTINEL.indexOf('export function readXrunSentinelMirror'),
		SENTINEL.indexOf('export function flushXrunSessionCounter')
	);
	assert.ok(read.length > 0, 'control: readXrunSentinelMirror was found');
	assert.match(read, /return \{ \.\.\._session, detector: _detector \};/);
});

test('the counter is readable before the sentinel is armed, and starts at the empty session', () => {
	const read = SENTINEL.slice(
		SENTINEL.indexOf('export function readXrunSessionCounter'),
		SENTINEL.indexOf('export function readXrunSentinelMirror')
	);
	assert.ok(read.length > 0, 'control: readXrunSessionCounter was found');
	assert.match(read, /return \{ \.\.\._session \};/);
	assert.doesNotMatch(read, /_node/, 'the read must not depend on the sentinel node');
	assert.match(SENTINEL, /^let _session: XrunSessionCounter = \{ \.\.\.EMPTY_XRUN_SESSION \};$/m);
});

test('xruns is a non-negative integer before and after reports, as the engine requires', () => {
	const report = { xruns: 3, parked: 0, callbacks: 740, worst_gap_ms: 25, window_ms: 2000, threshold_ms: 9.7, clock: 'audio' };
	const counters = [EMPTY_XRUN_SESSION, foldXrunReport(EMPTY_XRUN_SESSION, report)];
	counters.push(foldXrunReport(counters[1], report));
	assert.deepEqual(counters.map((counter) => counter.xruns), [0, 3, 6]);
	for (const counter of counters) {
		const wire = JSON.parse(JSON.stringify({ xrun_sentinel: counter }));
		assert.ok(Number.isInteger(wire.xrun_sentinel.xruns) && wire.xrun_sentinel.xruns >= 0);
	}
});
