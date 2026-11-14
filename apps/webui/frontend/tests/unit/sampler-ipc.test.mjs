/**
 * Issue #10786 (S9): each edge sampler bus command runs the store action its UI control runs,
 * and a command that cannot act fails out loud on the bus.
 *
 * Regression lines:
 * - if sampler_pin names a ref then PUT /pins carries the slot that ref shows
 * - if a ref or slot_id names no slot then the command rejects instead of doing nothing
 * - if sampler_enabled off runs then the pref is off and both edges sleep
 * - if sampler_trigger plays a ref then the engine's toggle receives that slot
 * - if a play arrives before an engine registers then the bus sees SamplerNoEngineError
 * - if sampler_stop all runs then every armed or playing slot stops, and only those
 * - if sampler_release runs on a gate slot then its voice does not stop, even when still loading
 * - if the performance bus does not route sampler_* to the sampler then the dispatch never reaches it
 * - if search, filter or results run then the edge state differs from what the top tab sets
 * - if slot_update sends a partial settings patch then the PATCH drops the slot's other settings
 * - if set_name or edit run then the set is not renamed or the editor does not open and close
 * - if capture runs on a deck with a loop then POST /slots lacks the loop, the set id or the index
 * - if capture runs with no loop or a missing set then something is posted or the bus sees no error
 * - if delete runs then DELETE /slots is not sent or the store keeps the deleted slot
 *
 * Each test loads a fresh store and handler in one bundle and passes a fetch that answers like
 * the sampler routes.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export * from '$lib/player/sampler/sampler-store.svelte';",
	"export { applySamplerCommand } from '$lib/player/sampler/sampler-ipc';",
	"export { uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { getDeckState } from '$lib/player/state.svelte';"
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

/** A fetch answering the sampler routes from memory; records pin writes and every other write. */
function samplerFetch(catalogue) {
	let pins = [];
	const writes = [];
	const sent = [];
	const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
	const fetchFn = async (url, init = {}) => {
		const u = new URL(String(url));
		const method = init.method ?? 'GET';
		const id = decodeURIComponent(u.pathname.split('/').pop());
		const body = init.body === undefined ? null : JSON.parse(init.body);
		if (method !== 'GET' && !u.pathname.includes('/pins/')) sent.push([method, id, body]);
		if (method === 'PATCH' && u.pathname.includes('/slots/')) {
			const i = catalogue.findIndex((x) => x.slot_id === id);
			catalogue[i] = { ...catalogue[i], ...body };
			return json(catalogue[i]);
		} else if (method === 'PUT' && u.pathname.includes('/sets/')) return json({ id, ...body });
		else if (method === 'POST' && u.pathname.endsWith('/slots')) {
			const made = { ...slot(90), slot_id: 'cap-1', source: 'capture', stable_id: body.stable_id };
			catalogue.push(made);
			return json(made, 201);
		} else if (method === 'DELETE' && u.pathname.includes('/slots/')) {
			catalogue.splice(
				catalogue.findIndex((x) => x.slot_id === id),
				1
			);
			return new Response(null, { status: 204 });
		} else if (method === 'GET' && u.pathname.endsWith('/slots')) {
			return json({
				slots: catalogue,
				total: catalogue.length,
				skipped_no_grid: 0,
				rekordbox: 'ok'
			});
		} else if (method === 'GET' && u.pathname.endsWith('/sets')) return json({ sets: [] });
		else if (method === 'GET' && u.pathname.endsWith('/pins')) return json({ pins });
		else if (method === 'PUT' && u.pathname.includes('/pins/')) {
			const id = decodeURIComponent(u.pathname.split('/').pop());
			const body = JSON.parse(init.body);
			writes.push({ id, pinned: body.pinned });
			pins = pins.filter((x) => x !== id).concat(body.pinned ? [id] : []);
			return json({ pins });
		}
		return new Response('no route', { status: 404 });
	};
	return { fetchFn, writes, sent };
}

/** An engine that records every call; `phases` sets what each slot's voice reads as. */
function fakeEngine(phases = {}) {
	const calls = [];
	return {
		calls,
		engine: {
			toggle: async (s) => void calls.push(['toggle', s.slot_id]),
			stop: (id) => void calls.push(['stop', id]),
			preview: async (s) => void calls.push(['preview', s.slot_id]),
			stopPreview: () => void calls.push(['stopPreview']),
			reside: () => undefined,
			phase: (id) => phases[id] ?? 'free'
		}
	};
}

async function loaded(n = 25) {
	const S = await loadRuneModule(ENTRY);
	const server = samplerFetch(Array.from({ length: n }, (_, i) => slot(i)));
	S.setSamplerFetch(server.fetchFn);
	globalThis.fetch = server.fetchFn; // delete calls the client API's default fetch
	await S.loadSampler();
	return { S, server };
}

test('pin by ref and by slot_id saves the slot the address names', async () => {
	/** [if] pin S1-02 on then slot_id s4 off run [then] PUT pins s1 true then s4 false, s1 stays pinned, [else stop]. */
	const { S, server } = await loaded();
	await S.applySamplerCommand({ type: 'sampler_pin', ref: 'S1-02', pinned: true });
	await S.applySamplerCommand({ type: 'sampler_pin', slot_id: 's4', pinned: false });
	assert.deepEqual(server.writes, [
		{ id: 's1', pinned: true },
		{ id: 's4', pinned: false }
	]);
	assert.equal(S.isPinned('s1'), true);
});

test('a ref or slot_id that names no slot rejects on the bus', async () => {
	/** [if] pin S9-99 or slot_id nope runs [then] it rejects naming the address and nothing is written, [else stop]. */
	const { S, server } = await loaded();
	await assert.rejects(
		S.applySamplerCommand({ type: 'sampler_pin', ref: 'S9-99', pinned: true }),
		/S9-99 names no slot/
	);
	await assert.rejects(
		S.applySamplerCommand({ type: 'sampler_pin', slot_id: 'nope', pinned: true }),
		/no slot nope/
	);
	assert.deepEqual(server.writes, []);
});

test('enabled off turns the pref off and puts both edges to sleep', async () => {
	/** [if] sampler_enabled off runs with L awake [then] the pref is off and L sleeps; on turns it back, [else stop]. */
	const { S } = await loaded();
	S.sampler.edges.L.awake = true;
	await S.applySamplerCommand({ type: 'sampler_enabled', on: false });
	assert.equal(S.uiPrefs.sampler_enabled, false);
	assert.equal(S.sampler.edges.L.awake, false);
	await S.applySamplerCommand({ type: 'sampler_enabled', on: true });
	assert.equal(S.uiPrefs.sampler_enabled, true);
});

test('trigger and preview reach the engine for the slot the address names', async () => {
	/** [if] trigger S1-03 and preview slot s4 run [then] the engine gets toggle s2 then preview s4, [else stop]. */
	const { S } = await loaded();
	const { engine, calls } = fakeEngine();
	S.registerSamplerEngine(engine);
	await S.applySamplerCommand({ type: 'sampler_trigger', ref: 'S1-03' });
	await S.applySamplerCommand({ type: 'sampler_preview', slot_id: 's4' });
	assert.deepEqual(calls, [
		['toggle', 's2'],
		['preview', 's4']
	]);
	await assert.rejects(
		S.applySamplerCommand({ type: 'sampler_trigger', ref: 'S9-99' }),
		/names no slot/
	);
});

test('a play or preview before any engine registers rejects with SamplerNoEngineError', async () => {
	/** [if] trigger or preview runs with no engine [then] the bus sees SamplerNoEngineError, not a quiet toast, [else stop]. */
	const { S } = await loaded();
	await assert.rejects(S.applySamplerCommand({ type: 'sampler_trigger', ref: 'S1-01' }), {
		name: 'SamplerNoEngineError'
	});
	await assert.rejects(S.applySamplerCommand({ type: 'sampler_preview', ref: 'S1-01' }), {
		name: 'SamplerNoEngineError'
	});
});

test('stop all stops only the armed and playing slots, and preview stop stops the preview', async () => {
	/** [if] s1 plays, s3 is armed, s5 previews [then] stop all stops s1 and s3, preview stop calls stopPreview, [else stop]. */
	const { S } = await loaded();
	const { engine, calls } = fakeEngine({ s1: 'playing', s3: 'armed', s5: 'previewing' });
	S.registerSamplerEngine(engine);
	await S.applySamplerCommand({ type: 'sampler_stop', all: true });
	await S.applySamplerCommand({ type: 'sampler_stop', ref: 'S1-08' });
	await S.applySamplerCommand({ type: 'sampler_preview', stop: true });
	assert.deepEqual(calls, [['stop', 's1'], ['stop', 's3'], ['stop', 's7'], ['stopPreview']]);
});

test('release stops a sounding gate slot and leaves loop slots playing', async () => {
	/** [if] release runs on gate s1 (playing) and loop s2 (playing) [then] only s1 stops; S9-99 rejects, [else stop]. */
	const { S } = await loaded();
	S.sampler.slots[1].settings = { ...S.sampler.slots[1].settings, mode: 'gate' };
	const { engine, calls } = fakeEngine({ s1: 'playing', s2: 'playing' });
	S.registerSamplerEngine(engine);
	await S.applySamplerCommand({ type: 'sampler_release', slot_id: 's1' });
	await S.applySamplerCommand({ type: 'sampler_release', slot_id: 's2' });
	assert.deepEqual(calls, [['stop', 's1']]);
	await assert.rejects(
		S.applySamplerCommand({ type: 'sampler_release', ref: 'S9-99' }),
		/names no slot/
	);
});

test('a gate release while the clip still loads stops the voice once it starts', async () => {
	/** [if] gate s1 is released before its toggle resolves [then] stop s1 follows the start, not before it, [else stop]. */
	const { S } = await loaded();
	S.sampler.slots[1].settings = { ...S.sampler.slots[1].settings, mode: 'gate' };
	const calls = [];
	let started;
	const loading = new Promise((resolve) => (started = resolve));
	S.registerSamplerEngine({
		...fakeEngine().engine,
		toggle: async (s) => {
			await loading;
			calls.push(['started', s.slot_id]);
		},
		stop: (id) => void calls.push(['stop', id])
	});
	const press = S.applySamplerCommand({ type: 'sampler_trigger', slot_id: 's1' });
	await S.applySamplerCommand({ type: 'sampler_release', slot_id: 's1' });
	assert.deepEqual(calls, [], 'nothing stops before the voice exists');
	started();
	await press;
	assert.deepEqual(calls, [
		['started', 's1'],
		['stop', 's1']
	]);
	await S.applySamplerCommand({ type: 'sampler_release', slot_id: 's1' });
	assert.equal(calls.length, 2, 'a second release finds nothing to stop');
});

test('set_page shows a set by index or flips by delta, and refuses an out-of-range set', async () => {
	/** [if] set_page L set 0 then R delta 1 run on 45 slots [then] L shows set 0, R set 1; set 7 rejects, [else stop]. */
	const { S } = await loaded(45);
	await S.applySamplerCommand({ type: 'sampler_set_page', edge: 'L', set: 0 });
	await S.applySamplerCommand({ type: 'sampler_set_page', edge: 'R', delta: 1 });
	assert.equal(S.sampler.edges.L.set, 0);
	assert.equal(S.sampler.edges.R.set, 1);
	await assert.rejects(
		S.applySamplerCommand({ type: 'sampler_set_page', edge: 'L', set: 7 }),
		/out of range/
	);
});

test('the performance bus routes sampler commands to the sampler store', async () => {
	/** [if] dispatchPerformanceCommand does not reach the sampler or skips its guard [then] agents cannot drive it, [else stop]. */
	const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'sampler_pin', ref: 'S1-01', pinned: true }),
			/S1-01 names no slot/
		);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({
				type: 'sampler_pin',
				ref: 'S1-01',
				slot_id: 'x',
				pinned: true
			}),
			/exactly one of slot_id or ref/
		);
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'sampler_set_page', edge: 'L', set: 3 }),
			/out of range/
		);
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'sampler_enabled', on: true }), null);
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'sampler_stop', all: true }), null);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('search, filter and results set the edge state the top tab sets', async () => {
	/** [if] search L, filter R vocals, results L show then hide run [then] each edge field follows, [else stop]. */
	const { S } = await loaded();
	await S.applySamplerCommand({ type: 'sampler_search', edge: 'L', query: 'Track 1' });
	await S.applySamplerCommand({ type: 'sampler_filter', edge: 'R', filter: 'vocals', on: true });
	assert.equal(S.sampler.edges.L.query, 'Track 1');
	assert.equal(S.sampler.edges.R.filters.vocals, true);
	await S.applySamplerCommand({ type: 'sampler_results', edge: 'L', show: true });
	assert.ok(S.sampler.edges.L.results.some((x) => x.slot_id === 's1'));
	await S.applySamplerCommand({ type: 'sampler_results', edge: 'L', show: false });
	assert.equal(S.sampler.edges.L.results, null);
});

test('slot_update merges a partial settings patch and renames through updateSlot', async () => {
	/** [if] level 50 then name Horn are sent for S1-02 [then] PATCH keeps the other settings and the slot renames, [else stop]. */
	const { S, server } = await loaded();
	await S.applySamplerCommand({
		type: 'sampler_slot_update',
		ref: 'S1-02',
		settings: { level: 50 }
	});
	await S.applySamplerCommand({ type: 'sampler_slot_update', slot_id: 's1', name: 'Horn' });
	const full = { ...slot(1).settings, level: 50 };
	assert.deepEqual(server.sent, [
		['PATCH', 's1', { settings: full }],
		['PATCH', 's1', { name: 'Horn' }]
	]);
	const s1 = S.sampler.slots.find((x) => x.slot_id === 's1');
	assert.equal(s1.name, 'Horn');
	assert.equal(s1.settings.level, 50);
});

test('set_name renames the set and edit opens then closes the editor', async () => {
	/** [if] set_name on the first set then edit S1-02 then close run [then] the set renames, editor s1 then null, [else stop]. */
	const { S, server } = await loaded();
	const setId = S.sampler.sets[0].id;
	await S.applySamplerCommand({
		type: 'sampler_set_name',
		set_id: setId,
		name: 'Warmup',
		description: ''
	});
	assert.equal(server.sent[0][2].name, 'Warmup');
	assert.equal(S.sampler.sets[0].name, 'Warmup');
	await S.applySamplerCommand({ type: 'sampler_edit', ref: 'S1-02' });
	assert.equal(S.sampler.editing, 's1');
	await S.applySamplerCommand({ type: 'sampler_edit', close: true });
	assert.equal(S.sampler.editing, null);
});

test('capture posts the deck loop into the named set and lays it into the store', async () => {
	/** [if] deck 2 loops 1000..3000 ms (4 beats) and capture set 0 index 3 runs [then] POST carries them, store has cap-1, [else stop]. */
	const { S, server } = await loaded();
	const deck = S.getDeckState(2);
	await assert.rejects(
		S.applySamplerCommand({ type: 'sampler_capture', deck: 2, set: 0, index: 3 }),
		/no engaged loop|no track loaded/
	);
	deck.stable_id = 'deck-track';
	deck.loop = { in_ms: 1000, out_ms: 3000, engaged: true, beat_length: 4 };
	await assert.rejects(
		S.applySamplerCommand({ type: 'sampler_capture', deck: 2, set: 9, index: 0 }),
		/no set at index 9/
	);
	assert.deepEqual(server.sent, []);
	await S.applySamplerCommand({ type: 'sampler_capture', deck: 2, set: 0, index: 3 });
	const [[method, , body]] = server.sent;
	assert.equal(method, 'POST');
	assert.deepEqual(
		[body.stable_id, body.in_ms, body.out_ms, body.beats, body.set_id, body.index],
		['deck-track', 1000, 3000, 4, S.sampler.sets[0].id, 3]
	);
	assert.ok(S.sampler.slots.some((x) => x.slot_id === 'cap-1'));
});

test('delete sends DELETE for the addressed slot and the reloaded store drops it', async () => {
	/** [if] delete S1-03 runs [then] DELETE /slots/s2 is sent and s2 is gone from the store, [else stop]. */
	const { S, server } = await loaded();
	await S.applySamplerCommand({ type: 'sampler_delete', ref: 'S1-03' });
	assert.deepEqual(server.sent, [['DELETE', 's2', null]]);
	assert.equal(
		S.sampler.slots.some((x) => x.slot_id === 's2'),
		false
	);
});
