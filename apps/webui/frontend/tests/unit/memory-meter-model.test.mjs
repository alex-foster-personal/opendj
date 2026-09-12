import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PERF-R5 Q10. PerfMeters._updateMemory read performance.memory.usedJSHeapSize
// unguarded. performance.memory is a Chromium-only API: in the shipping
// Tauri/WKWebView shell `'memory' in performance` is false, so jsHeapMB
// silently became 0 and the hover rendered "JS heap: 0 MB" as if it had been
// measured. The warn>512 / crit>1024 thresholds were calibrated for
// heap + PCM and were then sitting on PCM alone.
//
// Regression lines:
// - if readJsHeapMB stops returning null on a webview without performance.memory
//   then an unmeasured heap is reported as a measured 0 MB again
// - if the PCM-only readout drops its marker then the meter looks identical to a
//   measured one and nobody can tell the number is half a number
// - if the PCM-only thresholds revert to 512/1024 then a single stemmed deck
//   (~404 MiB of 5x mix PCM) is nearly enough to trip warn on its own
// - if the hover stops saying the heap is unavailable then the honest-state rule
//   is broken silently
// - if the heap-measured path stops summing heap + PCM then the meter
//   under-reports on Chromium

let model;

before(async () => {
	model = await loadTypeScriptModule('src/lib/rb/memory-meter-model.ts');
});

const PCM_ONLY = {
	jsHeapMB: null,
	pcmMB: 404,
	anlzMB: 12,
	anlzCount: 10,
	prefetchMB: 30,
	prefetchCount: 3
};

test('readJsHeapMB returns null when the webview has no performance.memory', () => {
	assert.equal(
		model.readJsHeapMB({ now: () => 0 }),
		null,
		'WKWebView has no performance.memory - reporting 0 MB there is a fabricated number'
	);
	assert.equal(model.readJsHeapMB(undefined), null);
	assert.equal(model.readJsHeapMB({ memory: {} }), null, 'a memory object with no reading is still no reading');
});

test('readJsHeapMB converts usedJSHeapSize to MiB on Chromium', () => {
	assert.equal(model.readJsHeapMB({ memory: { usedJSHeapSize: 268 * 1024 * 1024 } }), 268);
});

test('hasJsHeapApi feature-detects the Chromium-only API', () => {
	assert.equal(model.hasJsHeapApi({ memory: { usedJSHeapSize: 1 } }), true);
	assert.equal(model.hasJsHeapApi({}), false);
	assert.equal(model.hasJsHeapApi(null), false);
});

test('a PCM-only readout is marked and says so in the hover', () => {
	const out = model.memoryReadout(PCM_ONLY);
	assert.equal(out.heapMeasured, false);
	assert.equal(out.totalMB, 404, 'with no heap reading the total IS the decoded PCM');
	assert.ok(
		out.text.endsWith('*'),
		`a PCM-only figure must render a distinct marker, got ${out.text}`
	);
	assert.match(
		out.hover,
		/JS heap: unavailable on this webview/,
		'if the hover stops naming the missing reading then a half-number reads as a whole one'
	);
	assert.match(out.hover, /Chromium-only/);
	assert.match(out.hover, /decoded PCM only/);
	assert.doesNotMatch(
		out.hover,
		/JS heap: 0 MB/,
		'the old bug: an unmeasured heap rendered as a measured zero'
	);
	assert.match(
		out.hover,
		/NOT in the figure above/,
		'ANLZ + prefetch live in the heap, so with no heap reading they are outside the total'
	);
});

test('a heap-measured readout sums heap + PCM and carries no marker', () => {
	const out = model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268 });
	assert.equal(out.heapMeasured, true);
	assert.equal(out.totalMB, 268 + 404);
	assert.equal(out.text, '672M', 'a fully measured figure must not wear the PCM-only marker');
	assert.match(out.hover, /JS heap: 268 MB/);
	assert.match(out.hover, /JS heap \+ decoded PCM/);
});

test('the PCM-only thresholds are re-derived from 4-stemmed-deck arithmetic', () => {
	// A 4-min stereo 44.1 kHz Float32 mix is ~81 MiB; a stems-ready deck holds
	// the mix plus 4 stems, so ~404 MiB. 4 decks stemmed - the engine's
	// designed ceiling - is ~1616 MiB.
	assert.equal(model.PCM_MB_PER_STEMMED_DECK, 404);
	assert.equal(
		model.PCM_ONLY_WARN_MB,
		800,
		'warn sits just under 2 of 4 decks stemmed (2 x 404 = 808 MiB)'
	);
	assert.equal(
		model.PCM_ONLY_CRIT_MB,
		1600,
		'crit sits just under all 4 decks stemmed (4 x 404 = 1616 MiB)'
	);
	assert.ok(
		model.PCM_ONLY_WARN_MB > model.HEAP_PLUS_PCM_WARN_MB,
		'PCM-only thresholds must be HIGHER than the heap+PCM ones: the same number ' +
			'on a smaller measurement would fire warn on one stemmed deck'
	);
});

test('threshold selection switches with the measurement, not with the number', () => {
	const at = (pcmMB, jsHeapMB) => model.memoryReadout({ ...PCM_ONLY, pcmMB, jsHeapMB }).level;

	// PCM-only: one stemmed deck is normal, two is a warning, four is critical.
	assert.equal(at(404, null), 'ok', 'one stemmed deck alone must not warn on WKWebView');
	assert.equal(at(808, null), 'warn');
	assert.equal(at(1616, null), 'crit');
	assert.equal(at(800, null), 'ok', 'the threshold is exclusive - exactly at warn is still ok');

	// Heap + PCM keeps the calibrated 512/1024 pair.
	assert.equal(at(200, 268), 'ok');
	assert.equal(at(404, 268), 'warn');
	assert.equal(at(808, 268), 'crit');
});

test('the hover states which threshold pair is in force', () => {
	assert.match(model.memoryReadout(PCM_ONLY).hover, /Thresholds \(PCM only\): warn > 800 MB, crit > 1600 MB/);
	assert.match(
		model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268 }).hover,
		/Thresholds \(heap \+ PCM\): warn > 512 MB, crit > 1024 MB/
	);
});

test('every hover line explains the number next to it', () => {
	// CLAUDE.md house rule: numeric readouts carry a hover explaining what the
	// number is. Each breakdown line must name its source, not just a figure.
	const hover = model.memoryReadout(PCM_ONLY).hover;
	for (const line of ['ANLZ cache', 'Deck PCM', 'Audio prefetch', 'Approx retained']) {
		assert.ok(hover.includes(line), `the hover lost its "${line}" line`);
	}
});

test('LOW sample hover names tier and cap instead of UNCAPPED', () => {
	const hover = model.memoryReadout({
		...PCM_ONLY,
		anlzCount: 8,
		anlzCap: 8,
		tierLabel: 'LOW'
	}).hover;
	assert.ok(!hover.includes('UNCAPPED'));
	assert.match(hover, /8\/8 tracks, LOW/);
});
