// requirement: EDGESAMP-14362
/**
 * Issue #10785 (S8): the edge tab's triggers live in EdgeTabControls (play, repeat, preview
 * wave, pencil) around the tab's title and meta, and the MIX chip says when captured stem
 * settings wait for a stem-set (spec 7.4, mock variant D `.in`).
 *
 * Regression lines:
 * - if a tab with muted stems and no stem-set shows a plain MIX chip then the user expects stems that do not play
 * - if the split moves the title out from between repeat and waveform then the row stops matching the mock
 * - if the title or pencil lose their --treveal fade then a half-out tab shows a clipped title
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let ssr;
before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as EdgeWave } from '$lib/components/rb/sampler/EdgeWave.svelte';",
			"export { DEFAULT_WAVE } from '$lib/player/sampler/edge-wave';",
			"export * as ui from '$lib/player/sampler/sampler-store.svelte';",
			"export * as logic from '$lib/player/sampler/sampler-store-logic';",
			"export { render } from 'svelte/server';"
		].join('\n'),
		{ css: 'injected' }
	);
});

const UNITY = { gain_db: 0, mute: false, solo: false };
const stems = (bass) => ({ vocals: UNITY, drums: UNITY, bass, inst: UNITY });
const vector = (bass) => ({
	trim_db: 0,
	eq_db: { low: 0, mid: 0, high: 0 },
	filter: 0,
	stems: stems(bass),
	key_shift_st: 0,
	master_tempo: true
});
const slot = (i, o = {}) => ({
	slot_id: `s${i}`,
	stable_id: `t${i}`,
	source: 'capture',
	cue_letter: null,
	in_ms: 0,
	out_ms: 8000,
	beats: 16,
	native_bpm: 124,
	key: '8A',
	energy: null,
	color: '#3fa7ff',
	title: `Track ${i}`,
	artist: 'Artist',
	name: null,
	description: '',
	vector: vector({ gain_db: 0, mute: true, solo: false }),
	settings: {
		launch: 'bar',
		key: 'auto',
		tempo: 'match',
		mode: 'loop',
		repeat: 'loop',
		downbeat_offset: 0,
		level: 80
	},
	flags: { broken: false, remix: false, vocals: false, offline: false },
	peaks: { low: [0.5], mid: [0.4], high: [0.3] },
	stems_ready: false,
	stem_parts: null,
	...o
});

/** Server-render one component; returns its markup and all scoped CSS. */
function renderWith(component, props) {
	const { head, body } = ssr.render(component, { props });
	const css = [...head.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
	return { body, css };
}

/** Load a muted-bass capture (s0) and one whose stems are ready (s1) onto the R edge. */
async function loadTwo() {
	const json = (body) => new Response(JSON.stringify(body), { status: 200 });
	const catalogue = [slot(0), slot(1, { stems_ready: true, stem_parts: ['vocals', 'inst'] })];
	ssr.ui.setSamplerFetch(async (url) => {
		const u = new URL(String(url));
		if (u.pathname.endsWith('/slots'))
			return json({ slots: catalogue, total: 2, skipped_no_grid: 0, rekordbox: 'ok' });
		return json(u.pathname.endsWith('/sets') ? { sets: [] } : { pins: [] });
	});
	await ssr.ui.loadSampler();
	ssr.ui.setEdgeSet('R', 0);
}

test('a tab whose captured stems wait for a stem-set says so on its MIX chip', async () => {
	/** [if] a capture mutes bass with no stem-set, another has stems ready [then] only the first reads MIX . stems pending, [else stop]. */
	assert.equal(ssr.logic.stemsPending(slot(0)), true);
	assert.equal(ssr.logic.stemsPending(slot(0, { stems_ready: true })), false);
	assert.equal(ssr.logic.stemsPending(slot(0, { vector: vector(UNITY) })), false);
	assert.equal(ssr.logic.stemsPending(slot(0, { vector: null })), false);
	await loadTwo();
	const { body } = renderWith(ssr.EdgeWave, {
		edge: 'R',
		top: 28,
		height: 800,
		params: ssr.DEFAULT_WAVE,
		enabled: true,
		reduceMotion: true
	});
	const tabs = body.split(/(?=<div[^>]*class="wslot)/).filter((t) => /data-slot-id="/.test(t));
	assert.match(tabs[0], /data-testid="edge-tab-stems-pending"[^>]*>MIX \. stems pending</);
	assert.doesNotMatch(tabs[1], /stems pending/);
	assert.match(tabs[1], /class="kept[^"]*"[^>]*>MIX</);
});

test('the controls sit around the title in the mock order and keep their fades', async () => {
	/** [if] a filled tab renders [then] play, repeat, title, waveform, pencil run in that order, title and pencil fade on --treveal, [else stop]. */
	await loadTwo();
	const { body, css } = renderWith(ssr.EdgeWave, {
		edge: 'R',
		top: 28,
		height: 800,
		params: ssr.DEFAULT_WAVE,
		enabled: true,
		reduceMotion: true
	});
	const tab = body.split(/(?=<div[^>]*class="wslot)/).find((t) => /data-slot-id="s0"/.test(t));
	const row = tab.slice(tab.indexOf('class="in'));
	const pos = ['class="play', 'class="rep', 'class="txt', 'class="wv', 'class="pen'].map((c) =>
		row.indexOf(c)
	);
	assert.ok(
		pos.every((p) => p > 0),
		`all present: ${pos}`
	);
	assert.deepEqual(
		[...pos].sort((a, b) => a - b),
		pos,
		'mock DOM order'
	);
	assert.match(
		row.slice(0, pos[1]),
		/<\/button>\s*(<!---->)?\s*<button/,
		'play then repeat, siblings'
	);
	for (const sel of ['\\.txt', '\\.pen'])
		assert.match(
			css,
			new RegExp(`${sel}\\.svelte-[\\w-]+ \\{[^}]*opacity: ?var\\(--treveal, 0\\)`),
			sel
		);
});
