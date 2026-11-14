// requirement: EDGESAMP-14530
/**
 * Issue #11127 (S13): a deck loop dragged onto an edge tab saves as a capture slot with the deck's
 * mix as its vector, and sampler_capture on the bus runs the same capture path.
 *
 * Regression lines:
 * - if a loop drops on tab 3 of a full set then it is not slot 03 or the old last slot is lost
 * - if the capture posts then its vector does not carry the deck's EQ, filter, trim, key and MT
 * - if a drop lands off the tabs, on search results or is cancelled then something is posted
 * - if sampler_capture runs then its writes and layout differ from a drop of the same loop
 *
 * Each test loads a fresh store and passes a fetch that answers like the sampler routes.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"export * from '$lib/player/sampler/sampler-store.svelte';",
	"export { applySamplerCommand } from '$lib/player/sampler/sampler-ipc';",
	"export { toasts } from '$lib/stores.svelte';",
	"export { getDeckState, mixerState } from '$lib/player/state.svelte';"
].join('\n');

const slot = (i) => ({
	slot_id: `s${i}`,
	stable_id: `t${i}`,
	source: 'loop_hot_cue',
	cue_letter: 'A',
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
	vector: null,
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
	peaks: { low: [0.5], mid: [0.5], high: [0.5] },
	stems_ready: false,
	stem_parts: null
});

/** A fetch answering GET /slots, /sets, /pins, POST /slots and PUT /sets/{id}; records every write. */
function samplerServer(catalogue) {
	const writes = [];
	const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
	const fetchFn = async (url, init = {}) => {
		const u = new URL(String(url));
		const method = init.method ?? 'GET';
		const body = init.body === undefined ? null : JSON.parse(init.body);
		const id = decodeURIComponent(u.pathname.split('/').pop());
		if (method !== 'GET') writes.push([method, u.pathname, body]);
		if (method === 'POST' && u.pathname.endsWith('/slots')) {
			const { set_id: _s, index: _i, ...row } = body;
			return json({ ...slot(900), ...row, slot_id: 'cap-1', source: 'capture' }, 201);
		} else if (method === 'PUT' && u.pathname.includes('/sets/')) return json({ id, ...body });
		else if (method === 'GET' && u.pathname.endsWith('/slots'))
			return json({
				slots: catalogue,
				total: catalogue.length,
				skipped_no_grid: 0,
				rekordbox: 'ok'
			});
		else if (method === 'GET' && u.pathname.endsWith('/sets')) return json({ sets: [] });
		else if (method === 'GET' && u.pathname.endsWith('/pins')) return json({ pins: [] });
		return new Response('no route', { status: 404 });
	};
	return { writes, fetchFn };
}

/** A loaded store of `n` ranked slots whose deck 2 holds an engaged 4 beat loop and a set mix. */
async function storeWithDeckLoop(n = 45) {
	const S = await loadRuneModule(ENTRY);
	const server = samplerServer(Array.from({ length: n }, (_, i) => slot(i)));
	S.setSamplerFetch(server.fetchFn);
	await S.loadSampler();
	const deck = S.getDeckState(2);
	deck.stable_id = 'deck-track';
	deck.loop = { in_ms: 1000, out_ms: 3000, engaged: true, beat_length: 4 };
	deck.key_shift_semitones = 2;
	deck.master_tempo_enabled = true;
	Object.assign(S.mixerState.channels[2], { eq_low: 0, eq_mid: 0.5, eq_high: 0.75, filter: 0.25 });
	return { S, server };
}

test('a loop dropped on tab 3 of a full set lands there and nothing is lost', async () => {
	/** [if] deck 2's loop drops on R tab 3 of full set 1 [then] it is slot 03, s19 opens set 2, 46 slots laid out, [else stop]. */
	const { S, server } = await storeWithDeckLoop();
	S.beginLoopDrag(2, 400, 300);
	assert.equal(S.sampler.drag.bars, '1 bar');
	S.moveLoopDrag(1200, 200, { edge: 'R', index: 2 });
	assert.deepEqual(S.sampler.drag.over, { edge: 'R', index: 2 });
	const saved = await S.endLoopDrag({ edge: 'R', index: 2 }, false);
	assert.equal(saved.slot_id, 'cap-1');
	assert.equal(S.sampler.drag, null);
	assert.equal(S.edgeItems('R')[2].slot_id, 'cap-1');
	assert.equal(S.sampler.sets[1].items[0].slot_id, 's19');
	assert.equal(S.sampler.sets.flatMap((s) => s.items).filter(Boolean).length, 46);
	assert.equal(S.sampler.justDropped, 'cap-1');
	assert.deepEqual(
		server.writes.map(([m, p]) => `${m} ${p}`),
		['POST /api/v1/sampler/slots', 'PUT /api/v1/sampler/sets/set-1']
	);
	assert.ok(S.toasts.some((t) => /Saved to Set 1 slot 03, with the deck's EQ/.test(t.message)));
});

test('the capture posts the deck mix as its vector on the original grid', async () => {
	/** [if] deck 2 has EQ, filter, key +2 and MT on [then] POST carries its loop and that vector, no audio file, [else stop]. */
	const { S, server } = await storeWithDeckLoop();
	S.beginLoopDrag(2, 0, 0);
	await S.endLoopDrag({ edge: 'R', index: 0 }, false);
	const [, , body] = server.writes[0];
	assert.deepEqual(
		[body.stable_id, body.in_ms, body.out_ms, body.beats, body.set_id, body.index],
		['deck-track', 1000, 3000, 4, 'set-1', 0]
	);
	assert.equal(body.vector.key_shift_st, 2);
	assert.equal(body.vector.master_tempo, true);
	assert.ok(body.vector.eq_db.low < body.vector.eq_db.mid, JSON.stringify(body.vector.eq_db));
	assert.notEqual(body.vector.filter, 0);
	assert.equal('audio' in body || 'file' in body, false);
});

test('a drop off the tabs, on search results or cancelled posts nothing', async () => {
	/** [if] a drop misses every tab, lands on search results or is cancelled [then] nothing is written, two toasts say why, [else stop]. */
	const { S, server } = await storeWithDeckLoop();
	S.beginLoopDrag(2, 0, 0);
	assert.equal(await S.endLoopDrag(null, false), null);
	S.sampler.edges.L.results = [];
	S.beginLoopDrag(2, 0, 0);
	assert.equal(await S.endLoopDrag({ edge: 'L', index: 0 }, false), null);
	S.beginLoopDrag(2, 0, 0);
	assert.equal(await S.endLoopDrag({ edge: 'R', index: 0 }, true), null);
	assert.deepEqual(server.writes, []);
	assert.ok(S.toasts.some((t) => /drop the loop on a tab at either screen edge/.test(t.message)));
	assert.ok(S.toasts.some((t) => /clear the search first/.test(t.message)));
});

test('sampler_capture writes and lays out exactly what a drop of the same loop does', async () => {
	/** [if] sampler_capture {deck 2, set 0, index 2} runs [then] its writes and layout equal a drop on R tab 3, [else stop]. */
	const dropped = await storeWithDeckLoop();
	dropped.S.beginLoopDrag(2, 0, 0);
	await dropped.S.endLoopDrag({ edge: 'R', index: 2 }, false);
	const viaBus = await storeWithDeckLoop();
	await viaBus.S.applySamplerCommand({ type: 'sampler_capture', deck: 2, set: 0, index: 2 });
	assert.deepEqual(viaBus.server.writes, dropped.server.writes);
	const ids = (S) => S.sampler.sets.map((s) => s.items.map((x) => x?.slot_id ?? null));
	assert.deepEqual(ids(viaBus.S), ids(dropped.S));
});
