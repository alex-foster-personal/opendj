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
//   (~485 MiB of 6x mix PCM) is nearly enough to trip warn on its own
// - if the hover stops saying the heap is unavailable then the honest-state rule
//   is broken silently
// - if the heap-measured path stops summing heap + PCM then the meter
//   under-reports on Chromium

let model;
let heap;

before(async () => {
	model = await loadTypeScriptModule('src/lib/rb/memory-meter-model.ts');
	heap = await loadTypeScriptModule('src/lib/rb/js-heap-reading.ts');
});

const PCM_ONLY = {
	jsHeapMB: null,
	pcmMB: 485,
	anlzMB: 12,
	anlzCount: 10,
	prefetchMB: 30,
	prefetchCount: 3
};

test('readJsHeapMB returns null when the webview has no performance.memory', () => {
	assert.equal(
		heap.readJsHeapMB({ now: () => 0 }),
		null,
		'WKWebView has no performance.memory - reporting 0 MB there is a fabricated number'
	);
	assert.equal(heap.readJsHeapMB(undefined), null);
	assert.equal(heap.readJsHeapMB({ memory: {} }), null, 'a memory object with no reading is still no reading');
});

test('readJsHeapMB converts usedJSHeapSize to MiB on Chromium', () => {
	assert.equal(heap.readJsHeapMB({ memory: { usedJSHeapSize: 268 * 1024 * 1024 } }), 268);
});

test('hasJsHeapApi feature-detects the Chromium-only API', () => {
	assert.equal(heap.hasJsHeapApi({ memory: { usedJSHeapSize: 1 } }), true);
	assert.equal(heap.hasJsHeapApi({}), false);
	assert.equal(heap.hasJsHeapApi(null), false);
});

test('a PCM-only readout is marked and says so in the hover', () => {
	const out = model.memoryReadout(PCM_ONLY);
	assert.equal(out.heapMeasured, false);
	assert.equal(out.totalMB, 485, 'with no heap reading the total IS the decoded PCM');
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
	assert.equal(out.totalMB, 268 + 485);
	assert.equal(out.text, '753M', 'a fully measured figure must not wear the PCM-only marker');
	assert.match(out.hover, /JS heap: 268 MB/);
	assert.match(out.hover, /JS heap \+ decoded PCM/);
});

test('the PCM-only thresholds are re-derived from 4-stemmed-deck arithmetic', () => {
	// A 4-min stereo 44.1 kHz Float32 mix is ~81 MiB; a stems-ready deck holds
	// the mix plus 4 stems plus worklet copy, so ~485 MiB. 4 decks stemmed - the engine's
	// designed ceiling - is ~1940 MiB.
	assert.equal(model.PCM_MB_PER_STEMMED_DECK, 485);
	assert.equal(
		model.PCM_ONLY_WARN_MB,
		960,
		'warn sits just under 2 of 4 decks stemmed (2 x 485 = 970 MiB)'
	);
	assert.equal(
		model.PCM_ONLY_CRIT_MB,
		1920,
		'crit sits just under all 4 decks stemmed (4 x 485 = 1940 MiB)'
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
	assert.equal(at(485, null), 'ok', 'one stemmed deck alone must not warn on WKWebView');
	assert.equal(at(970, null), 'warn');
	assert.equal(at(1940, null), 'crit');
	assert.equal(at(960, null), 'ok', 'the threshold is exclusive - exactly at warn is still ok');

	// Heap + PCM keeps the calibrated 512/1024 pair.
	assert.equal(at(200, 268), 'ok');
	assert.equal(at(404, 268), 'warn');
	assert.equal(at(808, 268), 'crit');
});

test('the hover states which threshold pair is in force', () => {
	assert.match(model.memoryReadout(PCM_ONLY).hover, /Thresholds \(this page, PCM only\): warn > 960 MB, crit > 1920 MB/);
	assert.match(
		model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268 }).hover,
		/Thresholds \(this page, heap \+ PCM\): warn > 512 MB, crit > 1024 MB/
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

// the maintainer, Thu 15 Oct 2026: the meter read 0.4 GB while Chrome's Task Manager read
// 1.4 GB. The figure counted only what the page holds; the engine, its
// sidecars (odj-audio) and the desktop shell are separate processes it never
// added. The engine walks them (GET .../telemetry/processes) in every build,
// the browser one included.
//
// Regression lines:
// - if the process term stops being added then the meter under-reports the app
//   by the engine and every sidecar again
// - if the level starts judging the whole-app total then the page thresholds
//   (calibrated on GC pressure in the page) fire on the engine's resident set
// - if the page estimate is added on top of a measured page process then the
//   desktop shell double counts the page
// - if an unreadable process is summed as 0 then a missing reading looks measured

const ENGINE_SIDE = [
	{ label: 'opendj-engine', mb: 610, role: null },
	{ label: 'unnamed', mb: 42, role: null }
];

test('the figure adds the engine and sidecar processes to the page', () => {
	const out = model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes: ENGINE_SIDE });
	assert.equal(out.pageMB, 268 + 485);
	assert.equal(out.processesMB, 652);
	assert.equal(out.totalMB, 268 + 485 + 652, 'page + every measured app process');
	assert.equal(out.text, `${268 + 485 + 652}M`);
	assert.match(out.hover, /Approx resident: 1405 MB \(this page ~753 MB \(JS heap \+ decoded PCM\) \+ 652 MB in 2 engine and sidecar processes\)/);
	assert.match(out.hover, /• opendj-engine: 610 MB/);
	assert.match(out.hover, /• unnamed: 42 MB/);
	assert.match(out.hover, /Task Manager/, 'a browser page names what it cannot read about its own tab');
});

test('the level stays judged on the page, not on the whole app', () => {
	const big = [{ label: 'opendj-engine', mb: 5000, role: null }];
	assert.equal(model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 100, pcmMB: 100, processes: big }).level, 'ok');
	// control: the same page over its threshold still warns with the processes added
	assert.equal(model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes: big }).level, 'warn');
});

test('a measured page process replaces the page estimate instead of adding to it', () => {
	const desktop = [
		{ label: 'opendj-engine', mb: 600, role: null },
		{ label: 'opendj-webcontent', mb: 900, role: 'webkit-webcontent' }
	];
	const out = model.memoryReadout({ ...PCM_ONLY, processes: desktop });
	assert.equal(out.totalMB, 1500, 'the WebContent footprint already holds the heap and PCM');
	assert.equal(out.text, '1500M', 'a measured page process is not missing its heap term');
	assert.match(out.hover, /this page measured in its own process/);
	assert.doesNotMatch(out.hover, /Task Manager/);
	// The hover agrees with the number: the WebContent footprint holds the heap,
	// so nothing says the figure is PCM only or that heap lines are outside it.
	assert.doesNotMatch(out.hover, /decoded PCM only/);
	assert.doesNotMatch(out.hover, /NOT in the figure above/);
	assert.match(out.hover, /JS heap: not readable on this webview .*inside the measured page process/);
	assert.match(out.hover, /ANLZ cache: .*in heap\)/);
	// control: the same members without the page role add the estimate, and
	// then the heap really is outside the figure and the hover says so
	const browser = desktop.map((p) => ({ ...p, role: null }));
	const notInProcesses = model.memoryReadout({ ...PCM_ONLY, processes: browser });
	assert.equal(notInProcesses.totalMB, 1500 + 485);
	assert.match(notInProcesses.hover, /the figure above is decoded PCM only/);
	assert.match(notInProcesses.hover, /in heap - NOT in the figure above/);
});

test('under Electron the renderer in the family holds the page, so the page term is not added twice', () => {
	// What the engine's walk reports under the Electron shell (see
	// tests/shared/test_machine_pressure.py, real launch): the shell, its GPU
	// child, the engine, and the --type=renderer child marked electron-renderer.
	// Chromium reads the heap in-page, so heapMeasured is true here too.
	const electron = [
		{ label: 'unnamed', mb: 180, role: null },
		{ label: 'unnamed', mb: 120, role: null },
		{ label: 'opendj-engine', mb: 600, role: null },
		{ label: 'unnamed', mb: 700, role: 'electron-renderer' }
	];
	const out = model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes: electron });
	assert.equal(out.totalMB, 1600, 'the renderer resident MB already holds the heap and PCM');
	assert.match(out.hover, /this page measured in its own process/);
	assert.match(out.hover, /JS heap: 268 MB \(inside the measured page process listed below, not added again\)/);
	assert.doesNotMatch(out.hover, /Task Manager/);
	// control: plain Chrome, where no renderer is in the family; the page term is added
	const chrome = electron.filter((p) => p.role === null);
	const tab = model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes: chrome });
	assert.equal(tab.totalMB, 900 + 268 + 485);
	assert.match(tab.hover, /this page ~753 MB \(JS heap \+ decoded PCM\)/);
	assert.match(tab.hover, /• JS heap: 268 MB\n/);
	assert.match(tab.hover, /Task Manager/);
	assert.equal(out.level, tab.level, 'the level is judged on the page term either way');
});

test('an unreadable process is named and left out, never summed as 0', () => {
	const out = model.memoryReadout({
		...PCM_ONLY,
		jsHeapMB: 268,
		processes: [...ENGINE_SIDE, { label: 'unnamed', mb: null, role: null }]
	});
	assert.equal(out.processesMB, 652);
	assert.match(out.hover, /• unnamed: unavailable/);
	assert.match(out.hover, /1 process\(es\) could not be read - NOT in the figure above/);
	assert.match(out.hover, /in 2 engine and sidecar processes/);
});

test('with no process report the figure is the page alone and says so', () => {
	for (const processes of [null, undefined]) {
		const out = model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes });
		assert.equal(out.totalMB, 753);
		assert.equal(out.processesMB, null);
		assert.match(out.hover, /Engine and sidecar processes: unavailable \(the engine did not report them\) - NOT in the figure above/);
	}
	// an empty family is a real report, distinct from no report
	const empty = model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes: [] });
	assert.equal(empty.processesMB, 0);
	assert.doesNotMatch(empty.hover, /unavailable \(the engine/);
});

// PERF-UI-6785 (Codex review on #6785): a stale native-probe sample is left out
// of the members by the engine; the hover must say it was left out, so a
// missing footprint is not read as a measured small one.
test('a stale probe sample is named in the hover as left out of the figure', () => {
	const out = model.memoryReadout({
		...PCM_ONLY,
		jsHeapMB: 268,
		processes: ENGINE_SIDE,
		probeStale: { ageSeconds: 312.4 }
	});
	assert.equal(out.totalMB, 268 + 485 + 652, 'the stale sample adds nothing');
	assert.match(out.hover, /Native probe sample is stale \(312 s old\) - its process footprints are left out of the figure above/);
	const unknown = model.memoryReadout({ ...PCM_ONLY, processes: ENGINE_SIDE, probeStale: { ageSeconds: null } });
	assert.match(unknown.hover, /stale \(age unknown\)/);
	// control: a fresh (or absent) probe sample adds no such line
	for (const probeStale of [null, undefined]) {
		const fresh = model.memoryReadout({ ...PCM_ONLY, processes: ENGINE_SIDE, probeStale });
		assert.doesNotMatch(fresh.hover, /stale/);
	}
});

test('with no process report the hover still says a stale probe sample was left out', () => {
	const out = model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes: null, probeStale: { ageSeconds: 312.4 } });
	assert.equal(out.totalMB, 753, 'the stale sample adds nothing');
	assert.match(out.hover, /Engine and sidecar processes: unavailable/);
	assert.match(out.hover, /Native probe sample is stale \(312 s old\) - its process footprints are left out of the figure above/);
	// control: no stale sample, no such line
	assert.doesNotMatch(model.memoryReadout({ ...PCM_ONLY, jsHeapMB: 268, processes: null }).hover, /stale/);
});

test('a future-dated stale probe sample is named as ahead of the clock, not as negative seconds old', () => {
	const ahead = model.memoryReadout({ ...PCM_ONLY, processes: null, probeStale: { ageSeconds: -305.2 } });
	assert.match(ahead.hover, /Native probe sample is stale \(dated 305 s in the future\)/);
	assert.doesNotMatch(ahead.hover, /-305/);
	// control: a past sample still reads as its age
	const old = model.memoryReadout({ ...PCM_ONLY, processes: null, probeStale: { ageSeconds: 300.4 } });
	assert.match(old.hover, /Native probe sample is stale \(300 s old\)/);
});
