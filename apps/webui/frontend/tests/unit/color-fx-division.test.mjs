// requirement: COLORFX-10320
// [if] the channel is on ECHO and the color FX menu is open [then] it shows four menuitemradio division items (1/4, 1/2, 3/4, 1), the current one checked, each titled with its ms at the deck's tempo or the no-tempo reason
// [if] a division is chosen [then] it dispatches color_fx {deck, echo_beats} and the view reads echo_beats and echo_ms back while the knob does not move
// [if] the channel is on FILTER or SPACE [then] the division group is hidden and the division stays sticky for the next ECHO
// [if] Rust engine mode is on [then] every division item is inert by name (data-rust-command="color_fx")
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let division;
let fx;

before(async () => {
	division = await loadTypeScriptModule('src/lib/rb/color-fx-division.ts');
	fx = await loadSvelteSsrModule(`
		export { applyColorFxSettings, colorFxView, _resetColorFxForTests } from '$lib/rb/color-fx.svelte';
	`);
});

after(() => {
	fx?._resetColorFxForTests();
});

const STRIP = readFrontendSource('src/lib/components/rb/mixer/ChannelStrip.svelte');
const MIXER = readFrontendSource('src/lib/components/rb/Mixer.svelte');

function divisionGroup() {
	const start = STRIP.indexOf('{#if showsEchoDivisions(colorSettings.effect)}');
	assert.ok(start >= 0, 'the division group is gated on ECHO (control)');
	const end = STRIP.indexOf('{/if}', start);
	return STRIP.slice(start, end);
}

test('COLORFX-10320: the four divisions in menu order with their labels', () => {
	assert.deepEqual([...division.COLOR_FX_DIVISIONS], [0.25, 0.5, 0.75, 1]);
	assert.deepEqual(
		division.COLOR_FX_DIVISIONS.map((beats) => division.COLOR_FX_DIVISION_LABELS[beats]),
		['1/4', '1/2', '3/4', '1']
	);
});

test('COLORFX-10320: each item title states its ms at the deck tempo, or the no-tempo reason', () => {
	assert.equal(division.echoDivisionMs(0.75, 120), 375);
	assert.equal(division.echoDivisionMs(1, 128), 469);
	assert.equal(division.echoDivisionMs(0.25, null), null);
	assert.equal(
		division.echoDivisionTitle(0.5, 120),
		'ECHO 1/2 beat at 120.0 BPM = 250 ms per repeat'
	);
	assert.equal(
		division.echoDivisionTitle(0.75, null),
		'ECHO 3/4 beat - inert: this deck has no tempo'
	);
	assert.equal(
		division.echoDivisionTitle(1, Number.NaN),
		'ECHO 1 beat - inert: this deck has no tempo'
	);
});

test('COLORFX-10320: the group shows on ECHO only', () => {
	assert.equal(division.showsEchoDivisions('echo'), true);
	assert.equal(division.showsEchoDivisions('filter'), false);
	assert.equal(division.showsEchoDivisions('space'), false);
});

test('COLORFX-10320: the menu renders the divisions as checked menuitemradio items held inert by Rust', () => {
	const group = divisionGroup();
	assert.match(group, /\{#each COLOR_FX_DIVISIONS as beats \(beats\)\}/);
	assert.match(group, /role="menuitemradio"/);
	assert.match(group, /aria-checked=\{colorSettings\.echo_beats === beats\}/);
	assert.match(group, /title=\{echoDivisionTitle\(beats, colorSettings\.tempo_bpm\)\}/);
	assert.match(group, /data-rust-command="color_fx"/);
	assert.match(group, /onclick=\{\(\) => chooseEchoDivision\(beats\)\}/);
	// The group sits inside the open menu, after the effect items.
	const menu = STRIP.indexOf('class="color-fx-menu"');
	const effects = STRIP.indexOf('{#each COLOR_FX_EFFECTS as effect (effect)}', menu);
	assert.ok(
		menu >= 0 && effects > menu && STRIP.indexOf('{#if showsEchoDivisions', effects) > effects
	);
});

test('COLORFX-10320: a pick dispatches color_fx echo_beats only, never the knob value', () => {
	assert.match(STRIP, /if \(beats !== colorSettings\.echo_beats\) onechodivision\(beats\);/);
	assert.match(MIXER, /onechodivision=\{\(beats\) => handleEchoDivision\(deck, beats\)\}/);
	const handler = MIXER.slice(
		MIXER.indexOf('function handleEchoDivision'),
		MIXER.indexOf('function handleFader')
	);
	assert.match(handler, /runPerformanceCommandFromUi\(\{ type: 'color_fx', deck, echo_beats \}\)/);
	assert.doesNotMatch(handler, /value/, 'the division command carries no knob value');
});

test('COLORFX-10320: echo_beats reads back, the knob stays, and the division is sticky across effects', () => {
	fx._resetColorFxForTests();
	fx.applyColorFxSettings({ deck: 1, effect: 'echo' });
	assert.equal(fx.colorFxView()[1].echo_beats, 0.75, 'default division (control)');
	const view = fx.applyColorFxSettings({ deck: 1, echo_beats: 0.25 });
	assert.equal(view.effect, 'echo');
	assert.equal(view.echo_beats, 0.25);
	assert.equal(view.echo_ms, null, 'no deck tempo in this test, so no ms');
	fx.applyColorFxSettings({ deck: 1, effect: 'filter' });
	assert.equal(fx.colorFxView()[1].echo_beats, 0.25, 'FILTER keeps the division');
	fx.applyColorFxSettings({ deck: 1, effect: 'echo' });
	assert.equal(fx.colorFxView()[1].echo_beats, 0.25, 'switching back to ECHO restores it');
	assert.equal(fx.colorFxView()[2].echo_beats, 0.75, 'other decks untouched');
});
