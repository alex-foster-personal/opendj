/**
 * LYR-12 (orders board #5638 W40; the maintainer, Tue 6 Oct 2026: "move STAGE to being a
 * button within the LYR hover. Low Prio."): Stage is a button inside LYR's
 * hover menu, and that menu is LYR's ONE explainer.
 *
 * Regression lines:
 * - if the top bar has a standalone .topbar-slot-stage again, then Stage is back in the row
 * - if hover or keyboard focus on LYR stops opening the menu, then Stage is unreachable
 * - if LYR keeps a native title beside the menu, then it shows two explainers (W14)
 * - if clicking LYR stops toggling lyrics_global through set_lyrics_global, then the menu broke LYR itself
 * - if the menu's Stage button is not disabled with its reason when no deck is loaded, then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const TOPBAR = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)),
	'utf8'
);
const markup = TOPBAR.slice(TOPBAR.indexOf('</script>'), TOPBAR.indexOf('<style'));

function between(src, startNeedle, endNeedle) {
	const start = src.indexOf(startNeedle);
	assert.notEqual(start, -1, `${startNeedle} not found`);
	const end = src.indexOf(endNeedle, start);
	assert.notEqual(end, -1, `${endNeedle} not found after ${startNeedle}`);
	return src.slice(start, end);
}

test('the top bar has no standalone Stage slot', () => {
	assert.doesNotMatch(TOPBAR, /topbar-slot-stage/);
});

test('hover or keyboard focus on LYR opens the menu that holds Stage', () => {
	const wrap = between(markup, 'class="lyr-wrap topbar-slot-lyr-wrap"', '>');
	for (const handler of [
		'onpointerenter={_showLyrMenu}',
		'onpointerleave={_hideLyrMenu}',
		'onfocusin={_showLyrMenu}',
		'onfocusout={_hideLyrMenu}'
	]) {
		assert.ok(wrap.includes(handler), handler);
	}
	assert.match(wrap, /data-custom-tip=""/, 'tells the single-hover layer LYR draws its own hover');
	const menu = between(markup, '{#if lyrMenuOpen}', '{/if}');
	assert.match(menu, /class="ap-menu lyr-menu"/);
	assert.match(menu, /data-hover-card=""/);
	assert.match(menu, /class="bsm-toggle lyr-menu-stage"/);
});

test('LYR has one explainer: the menu, never a native title too', () => {
	const lyr = between(markup, 'class="bsm-toggle topbar-slot-lyr"', '</button>');
	assert.doesNotMatch(lyr, /\stitle=/);
	assert.match(lyr, /aria-describedby="lyr-menu-status"/);
	assert.match(markup, /id="lyr-menu-status"[^>]*>\{lyrTitle\}</);
});

test('clicking LYR dispatches the typed set_lyrics_global command', () => {
	const lyr = between(markup, 'class="bsm-toggle topbar-slot-lyr"', '</button>');
	assert.match(
		lyr,
		/onclick=\{\(\) =>\s*void runPerformanceCommandFromUi\(\{\s*type: 'set_lyrics_global',\s*enabled: !uiPrefs\.lyrics_global\s*\}\)\}/
	);
	assert.match(lyr, /aria-pressed=\{uiPrefs\.lyrics_global\}/);
});

test('Stage in the menu keeps its old behavior and says why it is disabled', () => {
	const stage = between(markup, 'class="bsm-toggle lyr-menu-stage"', '</button>');
	assert.match(stage, /disabled=\{stageTarget === null\}/);
	assert.match(stage, /onclick=\{_openStageFromTopBar\}/);
	assert.match(stage, /aria-describedby="lyr-menu-stage-status"/);
	assert.match(markup, /id="lyr-menu-stage-status"[^>]*>\s*\{stageTitle\}\s*</);
	assert.match(TOPBAR, /'Stage - load a track onto a deck first'/);
});
