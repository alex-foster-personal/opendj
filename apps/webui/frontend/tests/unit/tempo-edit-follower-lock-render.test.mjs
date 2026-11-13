/**
 * DECKUX-13420: what the tempo editor shows a Beat Sync follower, rendered
 * from real deck state (svelte SSR, no browser).
 *
 * - if a PLAYING follower is offered a type-in, TAP, Halve/Double or the
 *   slider without saying tempo is locked while it follows then broken
 * - if a STOPPED follower is shown a lock the engine does not apply then broken
 * - if a deck with no follower constraint shows a BPM range readout then broken
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { default as TempoEditModal } from '$lib/components/rb/deck/TempoEditModal.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

/** A trusted 4/4 grid with bar-1 downbeats, so Beat Sync is effective. */
function grid(bpm = 128, count = 64) {
	const beats = [];
	for (let index = 0; index < count; index += 1) {
		beats.push({ n: (index % 4) + 1, bpm, t: index * (60 / bpm) });
	}
	return { beatgrid: { source: 'rekordbox', status: 'ok', beats, beat_count: beats.length } };
}

function load(id, { playing = false, master = false, beatSync = false } = {}) {
	const deck = mod.deckStates[id];
	deck.stable_id = `fixture-${id}`;
	deck.bpm = 128;
	deck.pitch = 1;
	deck.anlz = grid();
	deck.playing = playing;
	deck.is_master = master;
	deck.beat_sync_enabled = beatSync;
}

beforeEach(() => {
	for (const id of [1, 2, 3, 4]) {
		const deck = mod.deckStates[id];
		deck.stable_id = null;
		deck.playing = false;
		deck.is_master = false;
		deck.beat_sync_enabled = false;
		deck.anlz = null;
	}
});

/** The rendered BPM <input> tag for one deck row. */
function bpmInput(html, id) {
	const tag = html.match(new RegExp(`<input[^>]*data-testid="tempo-edit-bpm-deck-${id}"[^>]*>`));
	assert.ok(tag, `deck ${id} renders a BPM input`);
	return tag[0];
}

function renderModal(deckId) {
	return mod.render(mod.TempoEditModal, { props: { deckId, x: 0, y: 0, onclose: () => {} } }).body;
}

// requirement: DECKUX-13420
test('a playing Beat Sync follower sees that tempo is locked and is offered no tempo edit', () => {
	load(1, { playing: true, master: true });
	load(2, { playing: true, beatSync: true });
	const html = renderModal(2);
	assert.match(
		html,
		/data-testid="tempo-edit-lock-deck-2"[^>]*>\s*Tempo locked while CH2 follows CH1 with Beat Sync: disable Beat Sync before changing follower deck 2/
	);
	assert.match(bpmInput(html, 2), /\sreadonly/);
	assert.doesNotMatch(html, /tempo-edit-slider-deck-2/);
	assert.doesNotMatch(html, /tempo-edit-tap-deck-2/);
	assert.doesNotMatch(html, /tempo-edit-lock-deck-1/, 'the master is free');
	assert.match(
		html,
		/data-testid="tempo-edit-sync-deck-2"/,
		'the Sync toggle is there to turn off'
	);
});

// requirement: DECKUX-14850
test('a stopped follower is not locked and keeps the type-in, TAP and slider', () => {
	load(1, { playing: true, master: true });
	load(2, { playing: false, beatSync: true });
	const html = renderModal(2);
	assert.doesNotMatch(html, /tempo-edit-lock-deck-2/);
	assert.doesNotMatch(bpmInput(html, 2), /\sreadonly/);
	assert.match(html, /data-testid="tempo-edit-slider-deck-2"/);
	assert.match(html, /data-testid="tempo-edit-tap-deck-2"/);
});

// requirement: DECKUX-13420
test('a deck with no follower constraint shows no range readout and no lock', () => {
	load(1, { playing: true, master: true });
	const html = renderModal(1);
	assert.doesNotMatch(html, /tempo-edit-lock-deck-/);
	assert.doesNotMatch(html, /tempo-edit-range-deck-/);
	assert.doesNotMatch(html, /Range \d/);
});
