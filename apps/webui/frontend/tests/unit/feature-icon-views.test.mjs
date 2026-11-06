import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// requirement: FICON-02
// The one-button feature strip (pin 87d7dac0a9b8) carries the view icons
// TOPVIEW-01..03 made live. Each must select the one perf_view pref, from the
// icon and from the strip's menu alike; FX has no panel yet and stays inert.

const cluster = await loadTypeScriptModule('src/lib/rb/feature-icon-cluster.svelte.ts');
const { PERF_VIEWS } = await loadTypeScriptModule('src/lib/rb/topbar-view-prefs.ts');
const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const TOPBAR = readFileSync(`${SRC}/lib/components/rb/TopBar.svelte`, 'utf8');

test('every view icon in the strip maps to a distinct real perf_view', () => {
	const ids = cluster.FEATURE_ICON_ROWS.flatMap((row) => row.icons.map((icon) => icon.id));
	const views = ids.map((id) => cluster.featureIconView(id)).filter(Boolean);
	assert.deepEqual([...views].sort(), [...PERF_VIEWS].sort(), 'each perf_view needs exactly one icon');
	// Control: the non-view members never map to a view.
	assert.equal(cluster.featureIconView('fx'), null);
	assert.equal(cluster.featureIconView('2-deck'), null);
});

test('FX has no panel yet, so it renders inert and toggles nothing', () => {
	assert.equal(cluster.toggleFeatureIcon, undefined, 'no page-local FX on/off state may come back');
	assert.equal(cluster.featureIconOn, undefined);
	const fxButton = TOPBAR.match(/<button[^>]*class="tb-icon fx rb-inert"[\s\S]*?>FX<\/button>/)?.[0] ?? '';
	assert.match(fxButton, /\bdisabled\b/);
	assert.doesNotMatch(fxButton, /\btitle=/, 'strip FX uses ControlExplainer only (issue #927)');
	assert.doesNotMatch(fxButton, /onclick|aria-pressed/);
	assert.match(TOPBAR, /const FX_INERT_TITLE = plannedTitle\('fx'\);/, 'the planned-control tooltip, not a bare stub');
	assert.match(TOPBAR, /disabled=\{icon\.id === 'fx'\}/, 'the strip menu entry is inert too');
	for (const id of ['list-view', '4-waveform', 'scope-1', 'scope-2']) {
		assert.doesNotMatch(TOPBAR, new RegExp(`toggleFeatureIcon\\('${id}'\\)`), `${id} is still a page-local toggle`);
	}
});

test('the strip menu routes a view icon through set_perf_view and lights it from the pref', () => {
	const pressed = TOPBAR.match(/function _featurePressed[\s\S]*?\n\t\}/)?.[0] ?? '';
	const toggle = TOPBAR.match(/function _toggleFeature[\s\S]*?\n\t\}/)?.[0] ?? '';
	assert.match(pressed, /featureIconView\(id\)[\s\S]*uiPrefs\.perf_view === view/);
	assert.match(toggle, /featureIconView\(id\)[\s\S]*runPerformanceCommandFromUi\(\{ type: 'set_perf_view', view \}\)/);
});

test('narrow and LESS tiers hide the overflow rows only at the default view', () => {
	assert.doesNotMatch(TOPBAR, /\t+\.rb-topbar(?:\.less-view)? \.icon-cluster-extra/, 'an overflow-row rule strands a chosen phase view');
	const gated = TOPBAR.match(/\.rb-topbar(?:\.less-view)?:not\(\.view-chosen\):not\(\.single-click\) \.icon-cluster-extra/g) ?? [];
	assert.equal(gated.length, 3, 'both width tiers and LESS must gate the overflow rows');
});

test('single-click mode never hides the view icons, because its menu is off', () => {
	// Sol P1, PR #6228: at the default view the narrow tiers hid the view icons and
	// the overflow rows while single-click mode had already disabled the menu.
	assert.match(TOPBAR, /class:single-click=\{featureIconSingleClick\(\)\}/);
	const viewRules = TOPBAR.match(/\.rb-topbar[^\n{]*\.icon-cluster > :global\(\.explainer:has\(\.tb-view\)\)/g) ?? [];
	assert.equal(viewRules.length, 2, 'both narrow tiers hide view icons at the default view');
	for (const rule of viewRules) assert.match(rule, /:not\(\.single-click\)/, rule);
	// Control: the menu really is off in single-click mode, which is why the icons must stay.
	assert.match(TOPBAR, /\{#if featureMenuOpen && !featureIconSingleClick\(\)\}/);
});

test('one-button mode makes every strip icon inert, so the cluster is the only control', () => {
	// FICON-01. Codex P2 BLOCKING, PR #6228: tabindex -1 kept the inner buttons in the
	// accessibility tree, so a screen reader could still press one directly.
	// From the cluster to the component menu, which must stay operable.
	const strip = TOPBAR.slice(TOPBAR.indexOf('class="icon-cluster"'), TOPBAR.indexOf('<div class="feature-menu"'));
	const buttons = strip.split('<button').slice(1).map((b) => b.slice(0, b.indexOf('onclick=')));
	assert.ok(buttons.length >= 6, `expected the strip's icon buttons, found ${buttons.length}`);
	assert.ok(buttons.some((b) => /class="tb-icon fx rb-inert"/.test(b)), 'FX is one of the checked buttons');
	for (const button of buttons) {
		// FX is disabled too (no panel yet), but disabled alone still leaves it
		// in screen-reader browse order, so it must be inert like the rest.
		assert.match(button, /inert=\{!featureIconSingleClick\(\)\}/, `not inert in one-button mode: ${button}`);
	}
	assert.doesNotMatch(strip, /tabindex=\{featureIconSingleClick\(\) \? undefined : -1\}/);
});
