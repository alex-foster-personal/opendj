// requirement: COLORFX-12718
// requirement: COLORFX-02
// requirement: COLORFX-15
// [if] the channel is on FILTER [then] the color knob carries the white face token (knob-face, --knob-face-color: var(--rb-color-fx-filter-face)) and no tint
// [if] the channel is on SPACE or ECHO [then] the color knob has no face override and keeps its tint
// requirement: COLORFX-8341
// [if] the selector is hovered [then] its title names the knob as a clock time and the cycle gesture
// Rendered (svelte/server) check of the channel strip's color FX selector, the
// dropdown the maintainer reported missing on main (Mon 2 Nov 2026).
// requirement: COLORFX-10
// [if] a channel strip renders [then] its color knob is a closed menu button (aria-haspopup=menu) with the chevron inside its caption and no native select
// [if] the deck's effect is SPACE [then] the knob caption reads SPACE
// [if] the deck's effect is SPACE or ECHO [then] the color knob carries data-rust-command="color_fx" in SSR when that effect is selected
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
		cueEnabled: false, mixerStems: false, stemPending: false,
		commandPending: false, commandError: null,
		ontrim: noop, onAutoGainOffset: asyncOk, oneq: noop, onfilter: noop, oncolorfx: noop, onechodivision: noop, oncolorfxall: noop, onfader: noop,
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


test('Color FX busy state and failure title follow the deck status, then recover', () => {
	mod._resetColorFxForTests();
	const busy = renderStrip({ ...props(2), commandPending: true });
	assert.match(busy, /aria-label="color FX deck 2" aria-busy="true"/);
	const failed = renderStrip({ ...props(2), commandError: 'Color FX deck 2: output refused', filter: 0.8 });
	assert.match(failed, /aria-busy="false"/);
	assert.match(failed, /title="Color FX deck 2: output refused"/);
	assert.match(triggerFor(failed, 2), />FILTER</);
	const recovered = renderStrip({ ...props(2), filter: 0.8 });
	assert.match(recovered, /aria-busy="false"/);
	assert.doesNotMatch(recovered, /output refused/);
	assert.match(recovered, /Color FX: FILTER, knob at/);
	assert.match(triggerFor(recovered, 2), /aria-valuenow="0.8"/);
});
