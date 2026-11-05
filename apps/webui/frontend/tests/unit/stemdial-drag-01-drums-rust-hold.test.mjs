// requirement: STEMDIAL-DRAG-01
/**
 * the maintainer, Wed 4 Nov 2026, live: the DRUMS stem dial does not turn when dragged, only on scroll; the
 * other stem dials drag fine.
 *
 * DRUMS is the only stem dial inside the colour FX slot (MIXUX-14: "DRUMS next to filter"). On a
 * SPACE or ECHO channel that slot carried data-rust-command="color_fx", and Rust engine mode's
 * capture guard (rust-inert.ts) eats every press under a held element, so the stem dial froze with
 * the colour knob. The hold belongs on the colour knob only: DRUMS sends stem_gain, which plays.
 * Real-Chromium proof: ChannelStrip.stories.ts StemsDrumsDragTurnsOnRustSpace.
 *
 * Regression lines:
 * - if the DRUMS dial renders inside any element carrying data-rust-command then Rust mode freezes its drag
 * - if a SPACE or ECHO colour knob loses data-rust-command="color_fx" then Rust mode drives the FILTER from it
 * - if a FILTER colour knob carries data-rust-command then the FILTER is held inert under Rust for no reason
 * - if the Cmd-click cycle handlers fire for a press outside the colour knob then a Cmd-press on DRUMS cycles the FX
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { bundleSvelteEntry } from './mount-svelte.mjs';
import { readFrontendSource } from './engine-source.mjs';

let bundle;

before(async () => {
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	bundle = await bundleSvelteEntry(`
		export { default as ChannelStrip } from '$lib/components/rb/mixer/ChannelStrip.svelte';
		export { deckStates } from '$lib/player/state.svelte';
		export { applyColorFxSettings, _resetColorFxForTests } from '$lib/rb/color-fx.svelte';
		export { render } from 'svelte/server';
	`, { 'import.meta.env.VITE_IO_DEVICE_ACCESS_ON_OPEN': 'undefined' });
});

after(() => {
	bundle?._resetColorFxForTests();
});

const ctl = () => ({ muted: false, solo: false, gain: 0.5 });
const noop = () => {};
const asyncNoop = async () => {};

function readyStems() {
	return {
		status: 'ready', source: 'demucs', model: 'htdemucs', layout: 'demucs4',
		available_controls: ['vocal', 'instrumental', 'drums', 'bass', 'other'], alignment: null,
		controls: { vocal: ctl(), instrumental: ctl(), drums: ctl(), bass: ctl(), other: ctl() }, error: null, load: null
	};
}

function renderStrip(deckId) {
	bundle.deckStates[deckId].stems = readyStems();
	const props = {
		deckId, less: false, trim: 0.5, eqHigh: 0.5, eqMid: 0.5, eqLow: 0.5, filter: 0.5, fader: 1,
		cueEnabled: false, mixerStems: true, stemPending: false,
		ontrim: noop, onAutoGainOffset: async () => ({ ok: true }), oneq: noop, onfilter: noop, oncolorfx: noop,
		onfader: noop, oncue: noop, onStemGain: noop, onStemMute: asyncNoop, onStemSolo: asyncNoop
	};
	return bundle.render(bundle.ChannelStrip, { props }).body;
}

/** Every element carrying data-rust-command, as [tag, its full outer HTML] (balanced same-tag walk). */
function rustHeldElements(html) {
	const out = [];
	for (const open of html.matchAll(/<(\w+)\b[^>]*\bdata-rust-command="([^"]+)"[^>]*>/g)) {
		const tag = open[1];
		const re = new RegExp(`<\\/?${tag}\\b[^>]*>`, 'g');
		re.lastIndex = open.index;
		let depth = 0;
		for (let m = re.exec(html); m !== null; m = re.exec(html)) {
			if (m[0].endsWith('/>')) continue;
			depth += m[0].startsWith('</') ? -1 : 1;
			if (depth === 0) {
				out.push({ command: open[2], html: html.slice(open.index, m.index + m[0].length) });
				break;
			}
		}
	}
	return out;
}

const DRUMS = (deck) => `aria-label="drums stem level deck ${deck}"`;
const FILTER = (deck) => `aria-label="filter deck ${deck}"`;

for (const effect of ['space', 'echo']) {
	test(`if a ${effect.toUpperCase()} channel's Rust hold covers the DRUMS dial then the drums drag freezes`, () => {
		bundle._resetColorFxForTests();
		bundle.applyColorFxSettings({ type: 'color_fx', deck: 2, effect });
		const html = renderStrip(2);
		assert.ok(html.includes(DRUMS(2)), 'control: S is on and the DRUMS dial renders');
		const held = rustHeldElements(html);
		const colour = held.filter((h) => h.command === 'color_fx' && h.html.includes(FILTER(2)));
		assert.equal(colour.length, 1, 'the colour knob is still held inert by name under Rust');
		for (const h of held) assert.ok(!h.html.includes(DRUMS(2)), `DRUMS sits inside a ${h.command} hold`);
		bundle._resetColorFxForTests();
	});
}

test('if a FILTER channel holds its colour knob inert under Rust then the FILTER is lost for no reason', () => {
	bundle._resetColorFxForTests();
	const html = renderStrip(1);
	assert.ok(html.includes(DRUMS(1)), 'control: the DRUMS dial renders');
	const held = rustHeldElements(html).filter((h) => h.html.includes(FILTER(1)) || h.html.includes(DRUMS(1)));
	assert.deepEqual(held, []);
});

test('if the Cmd-click cycle fires for a press outside the colour knob then a Cmd-press on DRUMS cycles the FX', () => {
	const strip = readFrontendSource('src/lib/components/rb/mixer/ChannelStrip.svelte');
	for (const name of ['handleColorSlotPointerDown', 'handleColorSlotMouseDown', 'handleColorSlotClick']) {
		const at = strip.indexOf(`function ${name}`);
		assert.notEqual(at, -1, `${name} exists`);
		const body = strip.slice(at, strip.indexOf('\n\t}', at));
		assert.match(body, /isColorFxCycleGesture\(event, cycleModifier\)/);
		assert.match(body, /pressOnColorKnob\(event\)/, `${name} only takes a press on the colour knob`);
	}
	assert.match(strip, /function pressOnColorKnob\(event: Event\): boolean \{\s*return colorFxTriggerEl !== null && event\.target instanceof Node && colorFxTriggerEl\.contains\(event\.target\);/);
});
