/**
 * PERF-UI-10: the top-bar performance readouts share one container and one
 * hover card that pins open on click.
 *
 * Regression lines:
 * - if perfReadoutRows does not return one row per compact readout, in display order, then broken
 * - if any row has an empty name or explainer, then the house numeric-readout rule is broken
 * - if a row invents a value instead of passing the live text through (-- stays --), then broken
 * - if a compact .perf-meter span carries its own title again, then the per-item tooltip is back
 * - if hover leave closes a pinned card, or a second click / Escape / outside click leaves it open, then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const PERF_METERS = fileURLToPath(new URL('../../src/lib/components/rb/PerfMeters.svelte', import.meta.url));

let card;

before(async () => {
	card = await loadTypeScriptModule('src/lib/rb/perf-readout-card.ts');
});

const LIVE = {
	hzText: '--',
	hzDetail: 'hz detail',
	cacheText: '0',
	cacheDetail: 'cache detail',
	waveformText: 'W--',
	waveformDetail: 'waveform detail',
	memoryText: '63M',
	memoryDetail: 'memory detail'
};

// -----------------------------------------------------------------------------
// Row model

export function assertRowModel(rows) {
	assert.deepEqual(
		rows.map((r) => r.key),
		['hz', 'cache', 'waveform', 'memory'],
		'one row per compact readout, in display order'
	);
	for (const row of rows) {
		assert.ok(row.name.trim().length > 0, `${row.key} has no name`);
		assert.ok(row.explainer.trim().length > 0, `${row.key} has no explainer`);
		assert.ok(row.detail.trim().length > 0, `${row.key} has no live detail`);
	}
}

test('perfReadoutRows returns one explained row per readout', () => {
	assertRowModel(card.perfReadoutRows(LIVE));
});

test('perfReadoutRows passes live values through untouched, including --', () => {
	const rows = card.perfReadoutRows(LIVE);
	assert.deepEqual(
		rows.map((r) => [r.key, r.value, r.detail]),
		[
			['hz', '--', 'hz detail'],
			['cache', '0', 'cache detail'],
			['waveform', 'W--', 'waveform detail'],
			['memory', '63M', 'memory detail']
		]
	);
});

test('mutation: a dropped row or a blank explainer makes the row check fail', () => {
	const rows = card.perfReadoutRows(LIVE);
	assert.throws(() => assertRowModel(rows.slice(0, 3)), /one row per compact readout/);
	const blank = rows.map((r) => (r.key === 'cache' ? { ...r, explainer: '  ' } : r));
	assert.throws(() => assertRowModel(blank), /cache has no explainer/);
});

// -----------------------------------------------------------------------------
// Pin state machine

function run(events) {
	return events.reduce((s, e) => card.nextPerfCardState(s, e), card.PERF_CARD_CLOSED);
}

test('hover opens the card and leaving closes it when not pinned', () => {
	assert.equal(card.isPerfCardOpen(run(['enter'])), true);
	assert.equal(card.isPerfCardOpen(run(['enter', 'leave'])), false);
});

test('a click pins the card so it stays open after the pointer leaves', () => {
	const pinned = run(['enter', 'toggle', 'leave']);
	assert.equal(pinned.pinned, true);
	assert.equal(card.isPerfCardOpen(pinned), true);
});

test('a second click closes the pinned card even with the pointer still over it', () => {
	assert.equal(card.isPerfCardOpen(run(['enter', 'toggle', 'toggle'])), false);
});

test('Escape closes a pinned card and a hover-only card', () => {
	assert.equal(card.isPerfCardOpen(run(['enter', 'toggle', 'leave', 'escape'])), false);
	assert.equal(card.isPerfCardOpen(run(['enter', 'escape'])), false);
});

test('a click outside unpins, and is a no-op when nothing is pinned', () => {
	assert.equal(card.isPerfCardOpen(run(['enter', 'toggle', 'leave', 'outside'])), false);
	const hoverOnly = run(['enter', 'outside']);
	assert.deepEqual({ ...hoverOnly }, { hovered: true, pinned: false });
});

test('keyboard pin without hover (Enter/Space reach toggle via the native button)', () => {
	const pinned = run(['toggle']);
	assert.equal(card.isPerfCardOpen(pinned), true);
	assert.equal(card.isPerfCardOpen(card.nextPerfCardState(pinned, 'toggle')), false);
});

// -----------------------------------------------------------------------------
// Component wiring (source scan: node tests never render Svelte)

function compactButton(src) {
	const start = src.indexOf('class="perf-meters-compact"');
	const end = src.indexOf('</button>', start);
	assert.ok(start > 0 && end > start, 'compact readout button not found');
	return src.slice(start, end);
}

export function assertNoPerItemTooltips(src) {
	const button = compactButton(src);
	const spans = button.match(/<span\b[^>]*class="perf-meter[^"]*"[^>]*>/g) ?? [];
	assert.equal(spans.length, 4, 'expected four compact readouts in one container');
	for (const span of spans) {
		assert.doesNotMatch(span, /\btitle=/, `per-item tooltip is back: ${span}`);
	}
	assert.doesNotMatch(button, /\btitle=/, 'the shared container must not carry a native title either');
}

test('the four readouts share one container and none carries a per-item title', () => {
	assertNoPerItemTooltips(readFileSync(PERF_METERS, 'utf8'));
});

test('mutation: restoring a per-item title makes the tooltip check fail', () => {
	const real = readFileSync(PERF_METERS, 'utf8');
	const mutated = real.replace('<span class="perf-meter cache-n">', '<span class="perf-meter cache-n" title={cacheHover}>');
	assert.notEqual(mutated, real, 'mutation did not apply');
	assert.throws(() => assertNoPerItemTooltips(mutated), /per-item tooltip is back/);
});

test('the container is a keyboard-reachable pin toggle wired to one card', () => {
	const src = readFileSync(PERF_METERS, 'utf8');
	const button = compactButton(src);
	assert.match(button, /aria-expanded=\{cardOpen\}/);
	assert.match(button, /aria-controls="perf-meters-panel"/);
	assert.match(button, /data-custom-tip=""/, 'tells the single-hover layer this control draws its own hover');
	assert.match(button, /onclick=\{\(\) => _cardEvent\('toggle'\)\}/);
	assert.match(src, /onpointerenter=\{\(\) => _cardEvent\('enter'\)\}/);
	assert.match(src, /onpointerleave=\{\(\) => _cardEvent\('leave'\)\}/);
	assert.match(src, /<svelte:window onpointerdown=\{_onWindowPointerDown\} onkeydown=\{_onWindowKeydown\}/);
	assert.match(src, /\{#if cardOpen\}\s*<div id="perf-meters-panel"/);
	assert.match(src, /\{row\.explainer\}/);
	assert.match(src, /\{row\.value\}/);
});
