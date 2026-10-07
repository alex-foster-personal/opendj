/**
 * PERF-UI-11 (orders board #5638 W18): the PERF-UI-10 readout card must not
 * be clipped by the top bar. `.rb-topbar` sets `overflow: hidden`, so a card
 * positioned `absolute` inside it showed at most 4.5px of a 600px card (Chromium,
 * 1440x900, Tue 6 Oct 2026). The card escapes the clip the way the mode menu
 * does: `position: fixed`, placed from the readouts by triggerFloatingAction.
 *
 * Regression lines:
 * - if the card's CSS position is anything but fixed, then the top bar clips it again
 * - if the card is not placed by triggerFloatingAction from the readouts, then a fixed card sits at 0,0
 * - if the card has no max-height with overflow-y: auto, then a pinned card runs off a short window
 * - if max-height leaves no room for the top bar, then the clamp lifts a pinned card over its own readouts
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const PERF_METERS = read('../../src/lib/components/rb/PerfMeters.svelte');
const TOP_BAR = read('../../src/lib/components/rb/TopBar.svelte');

/** Body of the first `selector { ... }` rule in a component's <style>. */
function ruleBody(source, selector) {
	const style = source.slice(source.indexOf('<style'));
	const at = style.indexOf(`${selector} {`);
	assert.notEqual(at, -1, `rule ${selector} not found`);
	return style.slice(at, style.indexOf('}', at));
}

test('the top bar still clips its overflow, so the card has to escape it', () => {
	// If this ever fails the clip is gone and the test below is guarding nothing.
	assert.match(ruleBody(TOP_BAR, '.rb-topbar'), /overflow:\s*hidden/);
	assert.match(TOP_BAR, /<PerfMeters\s*\/>/);
});

test('the readout card is fixed, not absolute', () => {
	const body = ruleBody(PERF_METERS, '.perf-meters-panel');
	assert.match(body, /position:\s*fixed/);
	assert.doesNotMatch(body, /position:\s*absolute/);
});

test('the card is placed from the readouts by the shared floating helper', () => {
	assert.match(PERF_METERS, /import \{ triggerFloatingAction \} from '\$lib\/ui\/clamp-to-viewport'/);
	const panel = PERF_METERS.slice(
		PERF_METERS.indexOf('id="perf-meters-panel"'),
		PERF_METERS.indexOf('}}', PERF_METERS.indexOf('use:triggerFloatingAction')) + 2
	);
	assert.match(panel, /use:triggerFloatingAction=\{\{\s*getTrigger: \(\) => rootEl/);
});

test('a pinned card taller than the window scrolls inside itself', () => {
	const body = ruleBody(PERF_METERS, '.perf-meters-panel');
	assert.match(body, /max-height:\s*calc\(100vh - var\(--rb-topbar-h, 28px\) - 12px\)/, 'room for the bar, so the card is never clamped over its readouts');
	assert.match(body, /overflow-y:\s*auto/);
});

/*
 * PERF-UI-12 (the maintainer, Tue 6 Oct 2026: "can we add charts without damaging
 * performance? Put a border around the score or move it to LHS col").
 *
 * - if a row's value is not in the bordered first column, then the numbers are hard to spot again
 * - if the explainer starts in the value column, then that column is no longer blank below the value
 * - if the closed-card tick stops recording history, then hovering shows an empty chart
 * - if the closed-card tick fetches the process family, then a closed card pays the pinned cost
 */
test('each value sits in a bordered left column and the text starts beside it', () => {
	assert.match(ruleBody(PERF_METERS, '.perf-readout-row'), /grid-template-columns:\s*3\.6em 1fr auto/);
	const value = ruleBody(PERF_METERS, '.perf-readout-value');
	assert.match(value, /grid-column:\s*1;/);
	assert.match(value, /border:\s*1px solid/);
	assert.match(ruleBody(PERF_METERS, '.perf-readout-name'), /grid-column:\s*2;/);
	assert.match(ruleBody(PERF_METERS, '.perf-readout-detail'), /grid-column:\s*2 \/ -1/);
});

test('each charted row draws its own history, and the closed card records it cheaply', () => {
	assert.match(PERF_METERS, /<PerfMeterSpark values=\{rowCharts\[row\.key\]\} \/>/);
	for (const key of ['hz', 'cache', 'memory']) {
		assert.match(PERF_METERS, new RegExp(`\\b${key}: chartHistory\\.map`), `${key} has a series`);
	}
	const closed = PERF_METERS.slice(PERF_METERS.indexOf('if (monitorOpen) return;'));
	const start = closed.slice(closed.indexOf('const start ='), closed.indexOf('const stop ='));
	assert.match(start, /setInterval\(\(\) => untrack\(_sampleTick\), intervalMs\)/);
	assert.doesNotMatch(start, /fetchProcessFamilySnapshot/);
});
