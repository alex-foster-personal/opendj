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
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const topbar = read('../../src/lib/components/rb/TopBar.svelte');
const page = read('../../src/routes/performance/+page.svelte');
const editMenu = read('../../src/lib/components/rb/browser/LibraryEditMenu.svelte');
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
		'.edit-menu-list > [data-less-hidden]',
		"[data-testid='playlist-set-tabs']"
	]) {
		assert.ok(m[1].includes(selector), `LESS must hide ${selector}`);
	}
	// MyTags is not one of the maintainer's named controls: the pencil menu (LIBUX-49) marks
	// exactly Find & Replace and Bulk Edit, and never MyTags.
	const buttons = editMenu.split('<button').slice(1).map((chunk) => chunk.split('</button>')[0]);
	const marked = buttons
		.filter((b) => b.includes('data-less-hidden'))
		.map((b) => b.match(/>\s*([A-Za-z][^<>]*?)\s*$/)?.[1]);
	assert.deepEqual(marked, ['Find &amp; Replace', 'Bulk Edit']);
});

// requirement: LESSV-03
// [if] a working sync shows text or the orange accent instead of cloud + tick in white [then] fail, [else stop]
test('a working sync is a cloud and a tick, in white', () => {
	assert.match(chip, /\{#if glyphs\.tick\}[\s\S]*?class="chip-tick"[\s\S]*?\{:else\}[\s\S]*?chip-label-full[\s\S]*?\{\/if\}/);
	const ok = chip.match(/\.chip\.ok\s*\{([^}]*)\}/);
	assert.ok(ok, 'a .chip.ok rule');
	assert.match(ok[1], /color:\s*var\(--fg\)/);
	assert.doesNotMatch(ok[1], /--accent/);
});

let view;
before(async () => {
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts');
});

function syncStatus(overrides = {}) {
	return {
		enabled: false,
		configured: false,
		running: false,
		heartbeat_at: null,
		enabled_source: 'default',
		endpoint_source: 'default',
		reason: null,
		signed_in_as: null,
		last_push_at: null,
		last_pull_at: null,
		last_result: null,
		rows_pending: null,
		endpoint: null,
		recent_results: [],
		update_required: null,
		...overrides
	};
}

// requirement: LESSV-03
// [if] a sync state maps to the wrong glyphs, text or colour [then] fail, [else stop]
test('sync chip state mapping: ok is cloud + tick in white, off/syncing/error keep their text', () => {
	const live = { configured: true, running: true, enabled: true };
	const cases = [
		['off', syncStatus(), 'sync: off', 'off'],
		['syncing', syncStatus(live), 'sync: syncing', 'sync'],
		['error', syncStatus({ ...live, last_result: { status: 'error', message: 'x' } }), 'sync: error', 'err'],
		['ok', syncStatus({ ...live, last_result: { status: 'ok', message: '' } }), null, null]
	];
	for (const [state, status, full, short] of cases) {
		assert.equal(view.chipState(status), state, `fixture for ${state}`);
		const glyphs = view.chipGlyphs(status);
		assert.equal(glyphs.cloud, true, `${state} always shows the cloud`);
		assert.equal(glyphs.tick, state === 'ok', `${state}: tick only when working`);
		assert.equal(glyphs.text, state !== 'ok', `${state}: text everywhere except ok`);
		if (full !== null) {
			assert.equal(view.chipFullLabel(status), full);
			assert.equal(view.chipShortLabel(status), short);
		}
	}
	assert.deepEqual(view.chipGlyphs(null), { cloud: true, tick: false, text: true }, 'loading reads as off');
	// The component renders exactly what chipGlyphs says: the cloud
	// unconditionally, the tick only under glyphs.tick, the labels otherwise,
	// and only the ok class is white (the shell foreground, never the accent).
	assert.match(chip, /<\/svg>\s*\{#if glyphs\.tick\}[\s\S]*?class="chip-tick"[\s\S]*?\{:else\}\s*<span class="chip-label-full">\{fullLabel\}<\/span>\s*<span class="chip-label-short">\{shortLabel\}<\/span>\s*\{\/if\}/);
	assert.match(chip, /class="chip-icon"/);
	assert.match(chip, /class:ok=\{chipState\(\) === 'ok'\}/);
	assert.match(chip, /class:error=\{chipState\(\) === 'error'\}/);
	const rule = (sel) => chip.match(new RegExp(`\\.chip${sel}\\s*\\{([^}]*)\\}`))?.[1] ?? '';
	assert.match(rule('\\.ok'), /color:\s*var\(--fg\)/);
	assert.match(rule('\\.error'), /color:\s*var\(--danger\)/);
	assert.match(rule(''), /color:\s*var\(--muted\)/, 'off and syncing use the muted base colour');
});
