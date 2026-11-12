// requirement: MIXUX-9832
// [if] the view is LESS [then] Tab visits fader, HI, MID, LOW, TRIM, CUE, FILTER in that order, and MORE still visits TRIM, HI, MID, LOW, FILTER, CUE, fader
//
// This file proves ChannelStrip DOM order per mode via SSR. Hidden-strip keyboard
// focus and the LESS dial release are proven in a real browser in
// tests/e2e/performance-less-mode-mixer-levels.spec.ts; releaseDeckControls itself
// is covered in knob-control.test.mjs.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let mod;

before(async () => {
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	mod = await loadSvelteSsrModule(`
		export { render } from 'svelte/server';
		export { default as ChannelStrip } from '$lib/components/rb/mixer/ChannelStrip.svelte';
	`);
});

const noop = () => {};

function props(deckId, less) {
	return {
		deckId,
		less,
		trim: 0.5,
		eqHigh: 0.5,
		eqMid: 0.5,
		eqLow: 0.5,
		filter: 0.5,
		fader: 1,
		cueEnabled: false,
		stemEqMode: false,
		stemPending: false,
		ontrim: noop,
		onAutoGainOffset: async () => ({ ok: true }),
		oneq: noop,
		onfilter: noop,
		oncolorfx: noop,
		onfader: noop,
		oncue: noop,
		onStemEqMode: noop,
		onStemGain: noop,
		onStemMute: async () => {},
		onStemSolo: async () => {}
	};
}

const renderStrip = (deckId, less) =>
	mod.render(mod.ChannelStrip, { props: props(deckId, less) }).body;

const VOID = new Set([
	'area',
	'br',
	'col',
	'embed',
	'hr',
	'img',
	'input',
	'link',
	'meta',
	'source',
	'track',
	'wbr'
]);

const attr = (tag, name) => {
	const m = tag.match(new RegExp(`\\s${name}(?:="([^"]*)")?(?=[\\s/>])`));
	return m === null ? null : (m[1] ?? '');
};

/** Sequential focus stops in document order, skipping tabindex="-1". Returns each stop's aria-label, else its data-testid. */
function tabStops(html) {
	const stops = [];
	const stack = [];
	html = html.replace(/<!--[\s\S]*?-->/g, '');
	for (const m of html.matchAll(/<(\/?)([a-zA-Z][\w-]*)([^>]*?)(\/?)>/g)) {
		const [tag, closing, rawName, , selfClose] = m;
		const name = rawName.toLowerCase();
		if (closing) {
			stack.pop();
			continue;
		}
		const tabindex = attr(tag, 'tabindex');
		const focusable =
			tabindex !== null
				? Number(tabindex) >= 0
				: (name === 'button' && attr(tag, 'disabled') === null) ||
					name === 'input' ||
					name === 'select' ||
					(name === 'a' && attr(tag, 'href') !== null);
		if (focusable) stops.push(attr(tag, 'aria-label') ?? attr(tag, 'data-testid') ?? name);
		if (VOID.has(name) || selfClose) continue;
		stack.push(true);
	}
	return stops;
}

function assertOrder(stops, expected, mode) {
	const at = expected.map((label) => stops.indexOf(label));
	at.forEach((index, i) =>
		assert.ok(
			index >= 0,
			`if ${mode} has no tab stop "${expected[i]}" then broken (stops: ${stops.join(' | ')})`
		)
	);
	for (let i = 1; i < at.length; i += 1) {
		assert.ok(
			at[i - 1] < at[i],
			`if ${mode} tabs to "${expected[i]}" before "${expected[i - 1]}" then broken`
		);
	}
}

test('LESS: Tab follows the strip grid, column by column', () => {
	const stops = tabStops(renderStrip(1, true));
	assertOrder(
		stops,
		[
			'channel fader deck 1',
			'high EQ deck 1',
			'mid EQ deck 1',
			'low EQ deck 1',
			'trim deck 1',
			'cue channel 1',
			'filter deck 1'
		],
		'LESS'
	);
	const stemAt = stops.findIndex(
		(label, i) => i > stops.indexOf('filter deck 1') && /stem|mute|solo|vocal|drum/i.test(label)
	);
	assert.ok(
		stemAt > stops.indexOf('filter deck 1'),
		'if the STEM chips are not after FILTER in LESS then broken'
	);
});

test('MORE: Tab order is unchanged (flex column, DOM order is visual order)', () => {
	assertOrder(
		tabStops(renderStrip(1, false)),
		[
			'trim deck 1',
			'high EQ deck 1',
			'mid EQ deck 1',
			'low EQ deck 1',
			'filter deck 1',
			'cue channel 1',
			'channel fader deck 1'
		],
		'MORE'
	);
});
