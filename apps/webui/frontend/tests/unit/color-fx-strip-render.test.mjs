// requirement: COLORFX-02
// requirement: COLORFX-15
// [if] the channel is on FILTER [then] the color knob carries the white face token (knob-face, --knob-face-color: var(--rb-color-fx-filter-face)) and no tint
// [if] the channel is on SPACE or ECHO [then] the color knob has no face override and keeps its tint
// requirement: COLORFX-8341
// [if] the color slot is Cmd-clicked (Ctrl-clicked off a Mac) [then] it dispatches color_fx effect next, and the press neither drags the knob, opens the select nor selects the deck
// [if] the selector is hovered [then] its title names the knob as a clock time and the cycle gesture
// Rendered (svelte/server) check of the channel strip's color FX selector, the
// dropdown the maintainer reported missing on main (Mon 2 Nov 2026).
// requirement: COLORFX-10
// [if] a channel strip renders [then] its color knob is a closed menu button (aria-haspopup=menu) with the chevron inside its caption and no native select
// [if] the deck's effect is SPACE [then] the knob caption reads SPACE
// [if] the deck's effect is SPACE or ECHO [then] the color knob (not its slot: DRUMS shares it, STEMDIAL-DRAG-01) carries data-rust-command="color_fx" so Rust engine mode holds it inert by name
// [if] the color FX menu opens [then] it offers FILTER, SPACE and ECHO as menuitemradio options that dispatch color_fx for that deck
// [if] SPACE or ECHO is selected [then] the knob caption reads the effect and the knob is tinted; FILTER keeps the default look
// [if] Rust engine mode is on [then] every menu option is inert by name (data-rust-command="color_fx")
// [if] Rust engine mode is on and the channel is on SPACE or ECHO [then] the color knob itself is inert by name
// [if] ECHO is selected [then] the chevron and menu hover title states the division, tempo and ms per repeat
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

function triggerFor(html, deckId) {
	const start = html.indexOf(`data-testid="color-fx-trigger-${deckId}"`);
	assert.ok(start >= 0, `deck ${deckId} renders a color FX menu trigger`);
	const open = html.lastIndexOf('<span', start);
	const end = html.indexOf('cue-btn', start);
	assert.ok(end > start, 'the CUE button follows the color knob slot (control)');
	return html.slice(open, end);
}

// COLORFX-10: the native select is gone (its popup ate the closing click); the
// knob itself is the menu button and the menu mounts only while open.
test('the strip renders the color knob as a closed menu button with the chevron in its caption', async () => {
	mod._resetColorFxForTests();
	const html = renderStrip(props(1));
	const trigger = triggerFor(html, 1);
	assert.match(trigger, /role="slider"/);
	assert.match(trigger, /aria-haspopup="menu"/);
	assert.match(trigger, /data-popup-open="false"/);
	assert.match(trigger, /class="label-text[^"]*">FILTER</);
	assert.match(trigger, /data-testid="color-fx-chevron-1"/, 'the chevron renders inside the caption, in flow');
	assert.doesNotMatch(html, /<select/, 'no native select: its popup swallows the next click');
	assert.doesNotMatch(html, /data-testid="color-fx-menu-1"/, 'the menu is closed until a tap');
	assert.doesNotMatch(html, /class="filter-knob[^"]*" data-rust-command="color_fx"/, 'a FILTER knob stays live under Rust');
});

test('selecting SPACE on one deck changes that strip only: caption, Rust hold', async () => {
	mod._resetColorFxForTests();
	mod.applyColorFxSettings({ type: 'color_fx', deck: 2, effect: 'space' });
	const html2 = renderStrip(props(2));
	assert.match(triggerFor(html2, 2), /class="label-text[^"]*">SPACE</, 'the knob caption reads the effect');
	assert.match(html2, /data-rust-command="color_fx"/);
	const html1 = renderStrip(props(1));
	assert.match(triggerFor(html1, 1), /class="label-text[^"]*">FILTER</, 'control: deck 1 still FILTER');
	mod._resetColorFxForTests();
});

// Drift pins on the wiring the render cannot see (handlers, the Rust hold attribute).
const STRIP = readFrontendSource('src/lib/components/rb/mixer/ChannelStrip.svelte');
const MIXER = readFrontendSource('src/lib/components/rb/Mixer.svelte');

test('the menu offers the shipped effect list, one option per effect, dispatching color_fx', () => {
	const menu = STRIP.slice(STRIP.indexOf('class="color-fx-menu"'), STRIP.indexOf('{/each}', STRIP.indexOf('class="color-fx-menu"')));
	assert.ok(menu.length > 0, 'the menu markup is present (control)');
	assert.match(menu, /role="menu"/);
	assert.match(menu, /aria-label=\{`color FX type deck \$\{deckId\}`\}/);
	assert.match(menu, /\{#each COLOR_FX_EFFECTS as effect \(effect\)\}/);
	assert.match(menu, /role="menuitemradio"/);
	assert.match(menu, /aria-checked=\{colorSettings\.effect === effect\}/);
	assert.match(menu, /onclick=\{\(\) => chooseColorFx\(effect\)\}/);
	assert.match(STRIP, /if \(effect !== colorSettings\.effect\) oncolorfx\(effect\);/);
});

test('every menu option is inert by name in Rust engine mode', () => {
	const option = STRIP.slice(STRIP.indexOf('role="menuitemradio"'), STRIP.indexOf('</button', STRIP.indexOf('role="menuitemradio"')));
	assert.ok(option.length > 0, 'the option is present (control)');
	assert.match(option, /data-rust-command="color_fx"/);
});

test('a SPACE or ECHO channel holds its color knob inert in Rust mode; FILTER stays live', () => {
	assert.match(STRIP, /class="filter-knob"\s+data-rust-command=\{colorSettings\.effect === 'filter' \? undefined : 'color_fx'\}/);
	// STEMDIAL-DRAG-01: never the slot, which also holds the DRUMS stem dial.
	assert.doesNotMatch(STRIP, /class="filter-slot"\s+data-rust-command=/);
});

test('the caption follows the effect and non-FILTER effects are tinted', () => {
	assert.match(STRIP, /label=\{COLOR_FX_LABELS\[colorSettings\.effect\]\}/);
	assert.doesNotMatch(STRIP, /label="FILTER"/, 'the caption is no longer hard-coded');
	assert.match(STRIP, /filter: undefined,/);
	assert.match(STRIP, /\{\.\.\.\(colorTint \? \{ accentColor: colorTint \} : \{\}\)\}/);
});

test('the echo title names its numbers on the chevron and the menu', () => {
	assert.equal(STRIP.match(/title=\{colorFxTitle\}/g)?.length, 2);
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
	// COLORFX-10: the menu replaced the native select; the caption chevron carries the title.
	const at = html.indexOf('data-testid="color-fx-chevron-1"');
	const select = html.slice(html.lastIndexOf('<span', at), html.indexOf('>', at) + 1);
	assert.match(select, /knob at 3 o&#39;clock|knob at 3 o'clock/);
	assert.match(select, /-click cycles FILTER, SPACE, ECHO/);
	assert.match(html, /data-testid="color-fx-slot-1"/);
});

// COLORFX-15 (MIXUX-02, the maintainer: "Filter dial should be white bg instead of black for clarity.")
test('COLORFX-15: a FILTER channel renders its color dial face white through the theme token', async () => {
	mod._resetColorFxForTests();
	const trigger = triggerFor(renderStrip(props(1)), 1);
	assert.match(trigger, /class="knob[^"]*\bknob-face\b/);
	assert.match(trigger, /--knob-face-color: var\(--rb-color-fx-filter-face\)/);
	assert.doesNotMatch(trigger, /--knob-accent-color/, 'FILTER carries no tint');
});

test('COLORFX-15: SPACE and ECHO keep the dark face plus their tint', async () => {
	for (const [effect, tint] of [['space', '#a99cff'], ['echo', '#6fd6c6']]) {
		mod._resetColorFxForTests();
		mod.applyColorFxSettings({ type: 'color_fx', deck: 2, effect });
		const trigger = triggerFor(renderStrip(props(2)), 2);
		assert.doesNotMatch(trigger, /knob-face/, `${effect} has no face override`);
		assert.match(trigger, new RegExp(`--knob-accent-color: ${tint}`), `${effect} keeps its tint`);
	}
	mod._resetColorFxForTests();
});

test('COLORFX-15: the filter face token is light in the dark and the light theme', async () => {
	const css = await readFrontendSource('src/lib/rb/theme.css');
	for (const head of ['.perf-root {', "html[data-theme='light'] .perf-root {"]) {
		const start = css.indexOf(`\n${head}`);
		assert.ok(start >= 0, `theme.css has ${head}`);
		const block = css.slice(start, css.indexOf('\n}', start));
		assert.match(block, /--rb-color-fx-filter-face: #f[0-9a-f]{5};/, `${head} defines a white filter face`);
	}
});
