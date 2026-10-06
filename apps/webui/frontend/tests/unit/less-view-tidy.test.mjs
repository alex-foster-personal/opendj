/**
 * requirement: LESSV-02, LESSV-03, LESSV-05
 *
 * Source-level guards for the maintainer's Tue 6 Oct 2026 LESS-view tidy. The rendered
 * proof is tests/e2e/performance-less-view-tidy.spec.ts; these pin the
 * contract so a refactor cannot quietly turn "hidden" into "removed".
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const topbar = read('../../src/lib/components/rb/TopBar.svelte');
const page = read('../../src/routes/performance/+page.svelte');
const chip = read('../../src/lib/components/CloudSyncStatusChip.svelte');

function lessViewTopbarRule() {
	const m = topbar.match(/((?:\t\.rb-topbar\.less-view [^\n]*,\n)+\t\.rb-topbar\.less-view [^\n]*\{ display: none; \})/);
	assert.ok(m, 'TopBar must carry one .rb-topbar.less-view display:none rule');
	return m[1];
}

// requirement: LESSV-02
// [if] the LESS view stops hiding Stage, voice command or an unfinished control [then] fail, [else stop]
test('the LESS top bar hides Stage, voice command and every unfinished control', () => {
	assert.match(topbar, /class:less-view=\{uiPrefs\.deck_layout === 'less'\}/);
	const rule = lessViewTopbarRule();
	for (const selector of [
		'.icon-cluster > :global(.explainer:has(.rb-inert))',
		'.link-btn',
		'.topbar-slot-pad',
		'.topbar-slot-utility',
		'.free-badge',
		'.topbar-slot-stage',
		':global(.cmd-entry)'
	]) {
		assert.ok(rule.includes(selector), `LESS must hide ${selector}`);
	}
	// Mutation guard: the 2-deck toggle is a real control and must survive.
	assert.equal(rule.includes('aria-pressed'), false);
	assert.doesNotMatch(rule, /\.icon-cluster\s*\{|\.icon-cluster > :global\(\.explainer\)[,\s]/);
});

// requirement: LESSV-02
// [if] a LESS-hidden top-bar control is unmounted by deck layout [then] fail, [else stop]
test('nothing in the top bar is unmounted by the deck layout', () => {
	assert.doesNotMatch(topbar, /\{#if[^}]*deck_layout/);
	assert.match(topbar, /<CommandEntry \/>/);
	assert.match(topbar, /class="bsm-toggle topbar-slot-stage"/);
});

// requirement: LESSV-02, LESSV-05
// [if] LESS stops hiding Find & Replace, Bulk Edit or the Set bar, or hides them in MORE [then] fail, [else stop]
test('Find & Replace, Bulk Edit and the Set bar are hidden by the LESS class only', () => {
	const m = page.match(/((?:\t\.perf-root\.deck-layout-less :global\([^\n]*,\n)+\t\.perf-root\.deck-layout-less :global\([^\n]*\{\n\t\tdisplay: none;)/);
	assert.ok(m, '+page.svelte must carry the LESS hide rule');
	for (const selector of [
		'.edit-actions-stack > .rb-lit-button',
		'.edit-actions-fold > .rb-lit-button:first-child',
		"[data-testid='playlist-set-tabs']"
	]) {
		assert.ok(m[1].includes(selector), `LESS must hide ${selector}`);
	}
	// MyTags (the fold's second button) is not one of the maintainer's named controls.
	assert.equal(m[1].includes('last-child'), false);
});

// requirement: LESSV-03
// [if] a working sync shows text or the orange accent instead of cloud + tick in white [then] fail, [else stop]
test('a working sync is a cloud and a tick, in white', () => {
	assert.match(chip, /\{#if chipState\(\) === 'ok'\}[\s\S]*?class="chip-tick"[\s\S]*?\{:else\}[\s\S]*?chip-label-full[\s\S]*?\{\/if\}/);
	const ok = chip.match(/\.chip\.ok\s*\{([^}]*)\}/);
	assert.ok(ok, 'a .chip.ok rule');
	assert.match(ok[1], /color:\s*var\(--fg\)/);
	assert.doesNotMatch(ok[1], /--accent/);
});
