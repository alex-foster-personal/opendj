/**
 * Issue #2607: the loop cluster RENDERED for a deck whose trusted grid has no
 * bar-1 downbeat.
 *
 * The first version of this coverage was regexes over LoopCluster.svelte,
 * which prove a token is in the file and nothing about what the component
 * renders. This compiles the real component through svelte's SSR renderer
 * (load-svelte-ssr.mjs) with the hotkey module stubbed, so every assertion is
 * about markup the component produced from the deck it was given. No DOM here,
 * so click handlers and later reactivity are out of scope; the engine side of
 * those clicks is gridless-beat-sync-phase.test.mjs.
 *
 * [if] the deck's trusted grid has no n===1 [then] the readout, halve, double,
 *   restart and every interval choice render disabled with the downbeat tip,
 *   and the readout still shows the default of 4 beats ⛔️
 * [if] the same deck has a real PQTZ grid [then] those controls render live
 *   with their working titles (control: the gate, not the render, is what
 *   disabled them above) ⛔️
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const HOTKEYS_STUB = fileURLToPath(
	new URL('./fixtures/loop-cluster-hotkeys-stub.ts', import.meta.url)
);

const ENTRY = [
	"export { default as LoopCluster } from '$lib/components/rb/deck/LoopCluster.svelte';",
	"export { loopIntervalView } from '$lib/rb/loop-interval-view.svelte';",
	"export { PHASE_FEATURE_TIP } from '$lib/player/grid-features';",
	"export { render } from 'svelte/server';"
].join('\n');

const BPM = 128;
const BEAT_SEC = 60 / BPM;

/** Cadence 2,3,4: the only validator-accepted shape with no bar-1. */
const NO_DOWNBEAT_BEATS = [2, 3, 4].map((n, index) => ({
	n,
	bpm: BPM,
	t: Number((0.1 + index * BEAT_SEC).toFixed(4))
}));

/** 64 beats, cadence 1,2,3,4: room for every interval choice offered. */
const REAL_BEATS = Array.from({ length: 64 }, (_, index) => ({
	n: 1 + (index % 4),
	bpm: BPM,
	t: Number((0.1 + index * BEAT_SEC).toFixed(4))
}));

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY, {
		alias: { '$lib/rb/performance-hotkeys': HOTKEYS_STUB }
	});
});

function deckWith(beats, loop = null) {
	return {
		deck_id: 1,
		stable_id: 'x'.repeat(40),
		position_ms: 600,
		duration_ms: 120_000,
		playing: false,
		loop,
		safety_loop: null,
		anlz: { beatgrid: { source: 'own', status: 'ok', beat_count: beats.length, beats } }
	};
}

const noop = async () => {};

function renderCluster(deck) {
	return mod.render(mod.LoopCluster, {
		props: {
			deck,
			deckId: 1,
			pending: false,
			onEngage: noop,
			onDisengage: noop,
			onSafetySave: noop,
			onSafetyArm: noop,
			onSafetyClear: noop,
			onIntervalMode: noop,
			onIntervalBase: noop,
			onStoredLoop: noop
		}
	}).body;
}

/** The one rendered `<button>` carrying `attribute="value"`, refusing zero or
 * several matches so an assertion never runs against the wrong control. */
function button(html, attribute, value) {
	const matches = [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
		.map((match) => match[0])
		.filter((tag) => tag.includes(`${attribute}="${value}"`));
	assert.equal(
		matches.length,
		1,
		`expected one button with ${attribute}="${value}", got ${matches.length}`
	);
	const [tag] = matches;
	const open = tag.slice(0, tag.indexOf('>') + 1);
	const title = /\stitle="([^"]*)"/.exec(open)?.[1] ?? null;
	const text = tag
		.slice(open.length, tag.lastIndexOf('</button>'))
		.replace(/<[^>]*>/g, '')
		.trim();
	return { disabled: /\sdisabled(?:=""|\s|>)/.test(open), title, text };
}

test('readout mode: readout, halve, double and restart render inert with the downbeat tip, readout still 4', () => {
	mod.loopIntervalView[1].gridMode = false;
	const html = renderCluster(deckWith(NO_DOWNBEAT_BEATS));
	const tip = mod.PHASE_FEATURE_TIP;

	const readout = button(html, 'data-testid', 'loop-deck-1');
	assert.equal(readout.disabled, true, 'the readout must not accept a press it cannot honor');
	assert.equal(readout.title, tip);
	assert.equal(readout.text, '4', 'the displayed default stays 4 beats');

	for (const testid of ['loop-halve-deck-1', 'loop-double-deck-1']) {
		const control = button(html, 'data-testid', testid);
		assert.equal(control.disabled, true, `${testid} must be inert`);
		assert.equal(control.title, tip, `${testid} must say why`);
	}

	const restart = button(html, 'aria-label', 'restart loop');
	assert.equal(restart.disabled, true);
	assert.equal(restart.title, tip);
});

test('readout mode: an engaged loop on that deck still leaves restart inert with the downbeat tip', () => {
	mod.loopIntervalView[1].gridMode = false;
	const html = renderCluster(
		deckWith(NO_DOWNBEAT_BEATS, { in_ms: 100, out_ms: 1037, engaged: true, beat_length: 2 })
	);
	const restart = button(html, 'aria-label', 'restart loop');
	assert.equal(
		restart.disabled,
		true,
		'restart re-engages through the beat-loop path and must not'
	);
	assert.equal(restart.title, mod.PHASE_FEATURE_TIP);
});

test('interval mode: every offered choice renders inert with the downbeat tip', () => {
	mod.loopIntervalView[1].gridMode = true;
	mod.loopIntervalView[1].gridBase = 4;
	try {
		const html = renderCluster(deckWith(NO_DOWNBEAT_BEATS));
		const choices = [4, 8, 16, 32].map((n) =>
			button(html, 'data-testid', `loop-${n}-beats-deck-1`)
		);
		for (const [index, choice] of choices.entries()) {
			assert.equal(choice.disabled, true, `choice ${[4, 8, 16, 32][index]} must be inert`);
			assert.equal(choice.title, mod.PHASE_FEATURE_TIP);
		}
		// The window shifters need no grid; they stay live so the DJ is not
		// locked into one window by a track that cannot loop yet.
		assert.equal(button(html, 'data-testid', 'loop-interval-up-deck-1').disabled, false);
	} finally {
		mod.loopIntervalView[1].gridMode = false;
		mod.loopIntervalView[1].gridBase = 4;
	}
});

test('control: a real PQTZ grid renders the same controls live with their working titles', () => {
	mod.loopIntervalView[1].gridMode = false;
	const html = renderCluster(deckWith(REAL_BEATS));

	const readout = button(html, 'data-testid', 'loop-deck-1');
	assert.equal(readout.disabled, false);
	assert.equal(readout.title, 'loop 4 beats');
	assert.equal(readout.text, '4');
	assert.equal(button(html, 'data-testid', 'loop-halve-deck-1').disabled, false);
	assert.equal(button(html, 'data-testid', 'loop-double-deck-1').disabled, false);
	// Restart still needs an engaged loop; its tip must name that, not the grid.
	const restart = button(html, 'aria-label', 'restart loop');
	assert.equal(restart.disabled, true);
	assert.equal(restart.title, 'engage a loop first - there is nothing to restart');

	mod.loopIntervalView[1].gridMode = true;
	try {
		const grid = renderCluster(deckWith(REAL_BEATS));
		for (const n of [4, 8, 16, 32]) {
			const choice = button(grid, 'data-testid', `loop-${n}-beats-deck-1`);
			assert.equal(choice.disabled, false, `choice ${n} must be live on a real grid`);
			assert.equal(choice.title, `loop ${n} beats`);
		}
	} finally {
		mod.loopIntervalView[1].gridMode = false;
	}
});
