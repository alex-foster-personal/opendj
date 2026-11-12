// requirement: PERF-CAPTURE-04
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import {
	CAP_HEADROOM,
	capFromMeasured,
	checkCaps,
	nearestRankPercentile,
	refusalReason,
	summarizeFrameIntervals
} from '../e2e/support/scroll-frame-stats.mjs';

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');

test('if deltas 16,16,17,33,16,70 do not give p95 70, max 70 and one long frame then broken', () => {
	const stats = summarizeFrameIntervals([16, 16, 17, 33, 16, 70]);
	assert.equal(stats.interval_p95_ms, 70);
	assert.equal(stats.interval_max_ms, 70);
	assert.equal(stats.long_frames, 1);
	assert.equal(stats.frames, 6);
});

test('if an empty series does not throw no frames sampled then broken', () => {
	assert.throws(() => summarizeFrameIntervals([]), /no frames sampled/);
	assert.throws(() => nearestRankPercentile([], 95), /no frames sampled/);
	assert.throws(() => summarizeFrameIntervals([16, Number.NaN]), /frame delta 1/);
});

test('if too few frames, no rows or no scroll are not refused with a reason then broken', () => {
	assert.match(refusalReason({ frames: 99, rowsMounted: 40, scrolledPx: 500 }), /99 frames.*100/);
	assert.match(
		refusalReason({ frames: 600, rowsMounted: 0, scrolledPx: 500 }),
		/no track row mounted/
	);
	assert.match(refusalReason({ frames: 600, rowsMounted: 40, scrolledPx: 0 }), /did not scroll/);
	assert.equal(refusalReason({ frames: 600, rowsMounted: 40, scrolledPx: 500 }), null);
});

test('if a p95 over its cap passes or omits the arithmetic then broken', () => {
	const [p95, listeners] = checkCaps(
		{ interval_p95_ms: 41.3, blocking_wheel_listeners: 0 },
		{ interval_p95_ms: 40, blocking_wheel_listeners: 0 }
	);
	assert.equal(p95.ok, false);
	assert.match(p95.arithmetic, /41\.3/);
	assert.match(p95.arithmetic, /40/);
	assert.equal(listeners.ok, true);
	assert.throws(() => checkCaps({}, { interval_p95_ms: 40 }), /interval_p95_ms/);
});

test('if the committed caps are not measured plus 20 percent then broken', () => {
	const fixture = JSON.parse(read('../fixtures/library-scroll-frames.json'));
	assert.deepEqual(Object.keys(fixture.caps).sort(), [
		'blocking_wheel_listeners',
		'interval_p95_ms'
	]);
	for (const [metric, cap] of Object.entries(fixture.caps)) {
		assert.equal(cap, capFromMeasured(metric, fixture.measured[metric]), metric);
	}
	assert.equal(fixture.headroom, CAP_HEADROOM);
});

test('if the stats module and its declaration file disagree then broken', async () => {
	const mod = await import('../e2e/support/scroll-frame-stats.mjs');
	const declaration = read('../e2e/support/scroll-frame-stats.d.mts');
	const declared = [...declaration.matchAll(/^export (?:function|const) (\w+)/gm)]
		.map((m) => m[1])
		.sort();
	assert.deepEqual(declared, Object.keys(mod).sort());
});

test('if the spec is not wired into the switch config unchanged, or refusals skip the JSON, then broken', () => {
	const config = read('../e2e/playwright.playlist-switch-latency.config.ts');
	assert.match(config, /'library-scroll-frames\.spec\.ts'/);
	assert.match(config, /\?\? 8713/);
	assert.match(config, /'playlist-switch-latency-data'/);
	assert.match(config, /playlist_switch_fixture\.py/);
	const spec = read('../e2e/library-scroll-frames.spec.ts');
	assert.match(spec, /DEFAULT_OUT_PATH = join\(FRONTEND_ROOT, 'test-results'/);
	assert.match(spec, /status: 'refused'/);
	assert.match(spec, /Refusing to write scroll bench results/);
});
