// requirement: COLORFX-02
// requirement: COLORFX-8341
// [if] the color slot is Cmd-clicked (Ctrl-clicked off a Mac) [then] it dispatches color_fx effect next, and the press neither drags the knob, opens the select nor selects the deck
// [if] the selector is hovered [then] its title names the knob as a clock time and the cycle gesture
// Rendered (svelte/server) check of the channel strip's color FX selector, the
// dropdown the maintainer reported missing on main (Mon 2 Nov 2026).
// [if] a channel strip renders [then] beside its color knob is a select offering FILTER, SPACE and ECHO, with the deck's effect selected
// [if] the deck's effect is SPACE [then] the knob caption reads SPACE and SPACE is the selected option
// [if] the deck's effect is SPACE or ECHO [then] the knob slot carries data-rust-command="color_fx" so Rust engine mode holds it inert by name
// [if] a channel strip renders [then] its color knob has a selector offering FILTER, SPACE and ECHO that dispatches color_fx for that deck
// [if] SPACE or ECHO is selected [then] the knob caption reads the effect and the knob is tinted; FILTER keeps the default look
// [if] Rust engine mode is on [then] the selector is inert by name (data-rust-command="color_fx")
// [if] Rust engine mode is on and the channel is on SPACE or ECHO [then] the knob slot itself is inert by name
// [if] ECHO is selected [then] the selector's hover title states the division, tempo and ms per repeat
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let mod;

before(async () => {
	// SSR runs the strip's onDestroy (a meter rAF loop teardown); node has no rAF.
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	mod = await loadSvelteSsrModule(`
		export { render } from 'svelte/server';
		export { default as ChannelStrip } from '$lib/components/rb/mixer/ChannelStrip.svelte';
		export { applyColorFxSettings, _resetColorFxForTests } from '$lib/rb/color-fx.svelte';
	`);
});

after(() => {
	mod?._resetColorFxForTests();
});

const noop = () => {};
const asyncOk = async () => ({ ok: true });

function props(deckId) {
	return {
		deckId, less: false, trim: 0.5, eqHigh: 0.5, eqMid: 0.5, eqLow: 0.5, filter: 0.5, fader: 1,
		cueEnabled: false, stemEqMode: false, stemPending: false,
		ontrim: noop, onAutoGainOffset: asyncOk, oneq: noop, onfilter: noop, oncolorfx: noop, onfader: noop,
		oncue: noop, onStemEqMode: noop, onStemGain: noop, onStemMute: async () => {}, onStemSolo: async () => {}
	};
}

function renderStrip(p) {
	return mod.render(mod.ChannelStrip, { props: p }).body;
}

function selectFor(html, deckId) {
	const start = html.indexOf(`data-testid="color-fx-select-${deckId}"`);
	assert.ok(start >= 0, `deck ${deckId} renders a color FX select`);
	const open = html.lastIndexOf('<select', start);
	return html.slice(open, html.indexOf('</select>', start) + '</select>'.length);
}

test('the strip renders a FILTER / SPACE / ECHO selector with FILTER selected by default', async () => {
	mod._resetColorFxForTests();
	const html = renderStrip(props(1));
	const select = selectFor(html, 1);
	assert.match(select, /aria-label="color FX type deck 1"/);
	const options = [...select.matchAll(/<option[^>]*value="([a-z]+)"[^>]*>([^<]*)</g)].map((m) => [m[1], m[2].trim()]);
	assert.deepEqual(options, [['filter', 'FILTER'], ['space', 'SPACE'], ['echo', 'ECHO']]);
	assert.match(select, /<option[^>]*value="filter"[^>]*selected/);
	assert.doesNotMatch(html, /class="filter-slot[^"]*" data-rust-command="color_fx"/, 'a FILTER slot stays live under Rust');
});

test('selecting SPACE on one deck changes that strip only: caption, selected option, Rust hold', async () => {
	mod._resetColorFxForTests();
	mod.applyColorFxSettings({ type: 'color_fx', deck: 2, effect: 'space' });
	const html2 = renderStrip(props(2));
	assert.match(selectFor(html2, 2), /<option[^>]*value="space"[^>]*selected/);
	assert.match(html2, />SPACE</, 'the knob caption reads the effect');
	assert.match(html2, /data-rust-command="color_fx"/);
	const html1 = renderStrip(props(1));
	assert.match(selectFor(html1, 1), /<option[^>]*value="filter"[^>]*selected/, 'control: deck 1 still FILTER');
	mod._resetColorFxForTests();
});

// Drift pins on the wiring the render cannot see (handlers, the Rust hold attribute).
const STRIP = readFrontendSource('src/lib/components/rb/mixer/ChannelStrip.svelte');
const MIXER = readFrontendSource('src/lib/components/rb/Mixer.svelte');

test('the strip renders one selector per channel over the shipped effect list', () => {
	assert.match(STRIP, /data-testid=\{`color-fx-select-\$\{deckId\}`\}/);
	assert.match(STRIP, /aria-label=\{`color FX type deck \$\{deckId\}`\}/);
	assert.match(STRIP, /\{#each COLOR_FX_EFFECTS as effect \(effect\)\}/);
	assert.match(STRIP, /value=\{colorSettings\.effect\}/);
	assert.match(STRIP, /onchange=\{\(event\) => oncolorfx\(event\.currentTarget\.value as ColorFxChoice\)\}/);
});

test('the selector is inert by name in Rust engine mode', () => {
	const select = STRIP.slice(STRIP.indexOf('<select'), STRIP.indexOf('</select>'));
	assert.ok(select.length > 0, 'the select is present (control)');
	assert.match(select, /data-rust-command="color_fx"/);
});

test('a SPACE or ECHO channel holds its whole knob slot inert in Rust mode; FILTER stays live', () => {
	assert.match(STRIP, /class="filter-slot"\s+data-rust-command=\{colorSettings\.effect === 'filter' \? undefined : 'color_fx'\}/);
});

test('the caption follows the effect and non-FILTER effects are tinted', () => {
	assert.match(STRIP, /label=\{COLOR_FX_LABELS\[colorSettings\.effect\]\}/);
	assert.doesNotMatch(STRIP, /label="FILTER"/, 'the caption is no longer hard-coded');
	assert.match(STRIP, /filter: undefined,/);
	assert.match(STRIP, /\{\.\.\.\(colorTint \? \{ accentColor: colorTint \} : \{\}\)\}/);
});

test('the echo title names its numbers', () => {
	assert.match(STRIP, /title=\{colorFxTitle\}/);
	assert.match(STRIP, /beat at \$\{bpm\.toFixed\(1\)\} BPM = \$\{ms\} ms per repeat/);
});

test('the mixer dispatches color_fx through the typed dispatcher for that deck', () => {
	assert.match(MIXER, /runPerformanceCommandFromUi\(\{ type: 'color_fx', deck, effect \}\)/);
	assert.match(MIXER, /oncolorfx=\{\(effect\) => handleColorFx\(deck, effect\)\}/);
});

test('COLORFX-8341: a Cmd-click anywhere on the color slot cycles, in capture, before the knob or select see it', () => {
	const slot = STRIP.slice(STRIP.indexOf('class="filter-slot"'), STRIP.indexOf('<Knob', STRIP.indexOf('class="filter-slot"')));
	assert.match(slot, /onpointerdowncapture=\{handleColorSlotPointerDown\}/);
	assert.match(slot, /onmousedowncapture=\{handleColorSlotMouseDown\}/);
	assert.match(slot, /onclickcapture=\{handleColorSlotClick\}/);
	const click = STRIP.slice(STRIP.indexOf('function handleColorSlotClick'), STRIP.indexOf('function handleColorSlotClick') + 400);
	assert.match(click, /isColorFxCycleGesture\(event, cycleModifier\)/);
	assert.match(click, /event\.stopPropagation\(\)/, 'not also a deck select');
	assert.match(click, /oncolorfx\(COLOR_FX_NEXT\)/);
	for (const name of ['handleColorSlotPointerDown', 'handleColorSlotMouseDown']) {
		const body = STRIP.slice(STRIP.indexOf(`function ${name}`), STRIP.indexOf(`function ${name}`) + 300);
		assert.match(body, /event\.preventDefault\(\);[\s\S]*event\.stopPropagation\(\)/, `${name} swallows the press`);
	}
	assert.match(STRIP, /const cycleModifier = quickPickModifier\(/);
});

test('COLORFX-8341 / DIALCLOCK-8341: the selector title names the knob as a clock time and the gesture', async () => {
	mod._resetColorFxForTests();
	const html = renderStrip({ ...props(1), filter: 5 / 6 });
	const select = selectFor(html, 1);
	assert.match(select, /knob at 3 o&#39;clock|knob at 3 o'clock/);
	assert.match(select, /-click cycles FILTER, SPACE, ECHO/);
	assert.match(html, /data-testid="color-fx-slot-1"/);
});
