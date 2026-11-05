// REQ: MIXUX-14
/**
 * The mixer's S button (the maintainer, Mon 2 Nov 2026): "The STEMS dials aren't working
 * in this build, they don't turn. The STEMS dials I think should stop toggling
 * per-track and instead have an S button just left of MORE/LESS that toggles
 * them on/off for all tracks that have them ... you can widen the mixer 20%
 * for them when on. So we'll have LOW MED HIGH right next to VOCAL INST BASS
 * and let's put DRUMS next to filter."
 *
 * Rendered through the real Svelte compiler and SSR (mount-svelte.mjs), with
 * the component and the stores it reads in ONE bundle.
 *
 * Regression lines:
 * - if S is off then no channel renders a stem level dial
 * - if S is on then a channel with ready stems renders VOCAL INST BASS after HI MID LOW and DRUMS after FILTER
 * - if S is on then a channel with no stems (or stems still loading) renders no stem dial
 * - if S is on then the EQ dials keep their own ids and labels (no swap under the pointer)
 * - if a RoFormer bundle has no BASS or DRUMS then those cells stay empty and no dial claims them
 * - if the S button is not immediately left of MORE/LESS then the layout ask is broken
 * - if the mixer_stems command does not set, persist and read back the pref then agents lose the S button
 * - if the mixer column does not widen exactly 20% with S on then the stem dials do not fit
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { bundleSvelteEntry } from './mount-svelte.mjs';

let bundle;

before(async () => {
	// SSR runs onDestroy, and the level meters' teardown cancels a frame.
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	bundle = await bundleSvelteEntry(`
		export { default as ChannelStrip } from '$lib/components/rb/mixer/ChannelStrip.svelte';
		export { default as Mixer } from '$lib/components/rb/Mixer.svelte';
		export { deckStates, mixerState } from '$lib/player/state.svelte';
		export { uiPrefs, setMixerStems } from '$lib/rb/prefs.svelte';
		export { stemDialLayout, channelShowsStemDials } from '$lib/rb/stem-dial-map';
		export { mixerColumnPx, MIXER_COLUMN_PX } from '$lib/rb/mixer-width';
		export { installPerformanceBrowserIpc, queryPerformanceState } from '$lib/rb/performance-ipc.svelte';
		// Render with the bundle's own svelte, so onDestroy and the SSR renderer share one runtime.
		export { render } from 'svelte/server';
	`, { 'import.meta.env.VITE_IO_DEVICE_ACCESS_ON_OPEN': 'undefined' });
});

const ctl = () => ({ muted: false, solo: false, gain: 0.5 });
const controls = () => ({ vocal: ctl(), instrumental: ctl(), drums: ctl(), bass: ctl(), other: ctl() });

function readyStems(layout) {
	return {
		status: 'ready',
		source: layout === 'demucs4' ? 'demucs' : 'roformer',
		model: layout === 'demucs4' ? 'htdemucs' : 'roformer',
		layout,
		available_controls: layout === 'demucs4' ? ['vocal', 'instrumental', 'drums', 'bass', 'other'] : ['vocal', 'instrumental'],
		alignment: null,
		controls: controls(),
		error: null,
		load: null
	};
}

function noStems(status = 'unavailable') {
	return {
		status,
		source: null,
		model: null,
		layout: null,
		available_controls: [],
		alignment: null,
		controls: controls(),
		error: null,
		load: null
	};
}

/** SSR body of a component, rendered by the bundle's own svelte runtime. */
function renderToHtml(component, props) {
	return bundle.render(component, { props }).body;
}

const noop = () => {};
const asyncNoop = async () => {};

function stripProps(deckId, mixerStems) {
	return {
		deckId,
		less: false,
		trim: 0.5,
		eqHigh: 0.5,
		eqMid: 0.5,
		eqLow: 0.5,
		filter: 0.5,
		fader: 1,
		cueEnabled: false,
		mixerStems,
		stemPending: false,
		ontrim: noop,
		onAutoGainOffset: async () => ({ ok: true }),
		oneq: noop,
		onfilter: noop,
		onfader: noop,
		oncue: noop,
		onStemGain: noop,
		onStemMute: asyncNoop,
		onStemSolo: asyncNoop
	};
}

/** aria-labels of every slider, in document order. */
function sliders(html) {
	return [...html.matchAll(/role="slider"[^>]*aria-label="([^"]+)"/g)].map((m) => m[1]);
}

/** The inner HTML of the first element with this class (balanced div walk). */
function block(html, cls) {
	const start = html.search(new RegExp(`<div class="${cls}[" ]`));
	assert.notEqual(start, -1, `no .${cls} rendered`);
	let depth = 0;
	const re = /<\/?div\b[^>]*>/g;
	re.lastIndex = start;
	for (let m = re.exec(html); m !== null; m = re.exec(html)) {
		depth += m[0].startsWith('</') ? -1 : 1;
		if (depth === 0) return html.slice(start, m.index + m[0].length);
	}
	throw new Error(`.${cls} never closed`);
}

async function renderStrip(deckId, stems, mixerStems) {
	bundle.deckStates[deckId].stems = stems;
	return renderToHtml(bundle.ChannelStrip, stripProps(deckId, mixerStems));
}

test('stem dial layout: VOCAL INST BASS beside HI MID LOW, DRUMS beside FILTER; absent controls leave a gap', () => {
	assert.deepEqual(bundle.stemDialLayout(['vocal', 'instrumental', 'drums', 'bass', 'other']), {
		column: ['vocal', 'instrumental', 'bass'],
		filterPartner: 'drums'
	});
	assert.deepEqual(bundle.stemDialLayout(['vocal', 'instrumental']), {
		column: ['vocal', 'instrumental', null],
		filterPartner: null
	});
	assert.equal(bundle.channelShowsStemDials(false, readyStems('demucs4')), false);
	assert.equal(bundle.channelShowsStemDials(true, readyStems('demucs4')), true);
	assert.equal(bundle.channelShowsStemDials(true, noStems('unavailable')), false);
	assert.equal(bundle.channelShowsStemDials(true, noStems('loading')), false);
});

test('S off: a channel with stems shows only its EQ, FILTER and TRIM dials', async () => {
	const html = await renderStrip(1, readyStems('demucs4'), false);
	assert.deepEqual(sliders(html), [
		'trim deck 1',
		'high EQ deck 1',
		'mid EQ deck 1',
		'low EQ deck 1',
		'filter deck 1',
		'channel fader deck 1'
	]);
	assert.match(html, /data-mixer-stems="false"/);
	assert.doesNotMatch(html, /stem level/);
	// The per-channel STEM toggle is gone; the heading over the mute chips stays.
	assert.doesNotMatch(html, /stem-mode-channel-1/);
	assert.doesNotMatch(html, /stem EQ mode channel/);
});

test('S on: stem dials stand beside the EQ (VOCAL INST BASS) and FILTER (DRUMS), EQ untouched', async () => {
	const html = await renderStrip(2, readyStems('demucs4'), true);
	assert.match(html, /data-mixer-stems="true"/);
	const eq = block(html, 'eq-stack');
	// Grid flows down then across: the EQ column, then the stem column.
	assert.deepEqual(sliders(eq), [
		'high EQ deck 2',
		'mid EQ deck 2',
		'low EQ deck 2',
		'vocal stem level deck 2',
		'inst stem level deck 2',
		'bass stem level deck 2'
	]);
	assert.deepEqual(sliders(block(html, 'filter-slot')), ['filter deck 2', 'drums stem level deck 2']);
	// EQ dials keep their ids: nothing swaps identity under the pointer.
	for (const role of ['high', 'mid', 'low']) assert.match(eq, new RegExp(`data-knob-id="2:${role}"`));
	for (const role of ['stem-vocal', 'stem-instrumental', 'stem-bass']) {
		assert.match(eq, new RegExp(`data-knob-id="2:${role}"`));
	}
	assert.match(html, /data-knob-id="2:stem-drums"/);
	// Stem dials carry the stem colours and the stem's own level.
	assert.match(eq, /knob-stem-accent/);
	assert.match(eq, /role="slider"[^>]*aria-label="vocal stem level deck 2"[^>]*aria-valuenow="0.5"/);
});

test('S on: a RoFormer bundle shows VOCAL and INST, leaves the BASS cell empty and no DRUMS', async () => {
	const html = await renderStrip(3, readyStems('roformer2'), true);
	const eq = block(html, 'eq-stack');
	assert.deepEqual(sliders(eq), [
		'high EQ deck 3',
		'mid EQ deck 3',
		'low EQ deck 3',
		'vocal stem level deck 3',
		'inst stem level deck 3'
	]);
	assert.match(eq, /class="stem-dial-empty/);
	assert.deepEqual(sliders(block(html, 'filter-slot')), ['filter deck 3']);
});

test('S on: channels without stems, or with stems still loading, show no stem dial', async () => {
	for (const stems of [noStems('unavailable'), noStems('loading')]) {
		const html = await renderStrip(4, stems, true);
		assert.match(html, /data-mixer-stems="false"/);
		assert.doesNotMatch(html, /stem level/);
	}
});

test('Mixer: S sits immediately left of MORE/LESS and drives only the channels that have stems', async () => {
	bundle.deckStates[1].stems = readyStems('demucs4');
	bundle.deckStates[2].stems = noStems();
	bundle.deckStates[3].stems = readyStems('roformer2');
	bundle.deckStates[4].stems = noStems('loading');

	bundle.uiPrefs.mixer_stems = false;
	let html = await renderToHtml(bundle.Mixer, {});
	const row = block(html, 'mixer-view-row');
	const sAt = row.indexOf('data-testid="mixer-stems-toggle"');
	const moreAt = row.search(/>\s*MORE\s*</);
	assert.ok(sAt !== -1 && moreAt !== -1 && sAt < moreAt, 'S must come before MORE in the same row');
	assert.ok(row.search(/class="deck-layout-toggle[" ]/) > sAt, 'S sits outside, and left of, the MORE/LESS group');
	assert.match(row, /data-testid="mixer-stems-toggle"[^>]*aria-pressed="false"|aria-pressed="false"[^>]*data-testid="mixer-stems-toggle"/);
	assert.doesNotMatch(html, /stem level/);

	bundle.uiPrefs.mixer_stems = true;
	html = await renderToHtml(bundle.Mixer, {});
	assert.match(block(html, 'mixer-view-row'), /aria-pressed="true"/);
	assert.match(html, /vocal stem level deck 1/);
	assert.match(html, /drums stem level deck 1/);
	assert.match(html, /vocal stem level deck 3/);
	assert.doesNotMatch(html, /drums stem level deck 3/);
	assert.doesNotMatch(html, /stem level deck 2/);
	assert.doesNotMatch(html, /stem level deck 4/);
	bundle.uiPrefs.mixer_stems = false;
});

test('mixer column widens exactly 20% with S on', () => {
	assert.equal(bundle.MIXER_COLUMN_PX, 248);
	assert.equal(bundle.mixerColumnPx(false), 248);
	assert.equal(bundle.mixerColumnPx(true), 248 * 1.2);
});

test('mixer_stems command sets, persists and reads back the S button (agent parity)', async () => {
	const store = new Map();
	const storage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, v),
		removeItem: (k) => store.delete(k)
	};
	const original = globalThis.localStorage;
	globalThis.localStorage = storage;
	globalThis.window = { localStorage: storage };
	const uninstall = bundle.installPerformanceBrowserIpc();
	try {
		assert.equal(bundle.queryPerformanceState().ui.mixer_stems, false);
		await window.musicDjToolsPerformance.dispatch({ type: 'mixer_stems', enabled: true });
		assert.equal(bundle.queryPerformanceState().ui.mixer_stems, true);
		assert.equal(bundle.uiPrefs.mixer_stems, true);
		assert.equal(JSON.parse(store.get('mdt.rb.ui-prefs.v1')).mixer_stems, true);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'mixer_stems', enabled: 'yes' }),
			/enabled must be boolean/i
		);
		await window.musicDjToolsPerformance.dispatch({ type: 'mixer_stems', enabled: false });
		assert.equal(bundle.queryPerformanceState().ui.mixer_stems, false);
	} finally {
		uninstall();
		delete globalThis.window;
		if (original === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = original;
	}
});
