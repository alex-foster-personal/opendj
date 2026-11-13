// requirement: EDGESAMP-14054, EDGESAMP-14158
/**
 * Issue #10784 (S7): the edge sampler store core loads the ranked catalogue through the API
 * client, lays out sets per edge, resolves refs, runs the actions and plays through the engine seam.
 *
 * Regression lines:
 * - if the server holds 2,000 slots then every page is read and each edge holds one set of 20
 * - if the catalogue loads then S2-03 resolves to the third slot of the second set
 * - if a load fails then the store toasts, rethrows and stays unloaded
 * - if one edge pages then the other edge keeps its set
 * - if a pin or a slot edit fails to save then it rolls back and toasts
 * - if a set is renamed then the name and description survive a reload and title the edge
 * - if a tab fires with no engine registered then it stays not-playing and an error toasts
 *
 * Each test loads a fresh store and passes its own fetch that answers like the sampler routes.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = [
	"export * from '$lib/player/sampler/sampler-store.svelte';",
	"export { toasts } from '$lib/stores.svelte';"
].join('\n');

const SETTINGS = {
	launch: 'bar',
	key: 'auto',
	tempo: 'match',
	mode: 'loop',
	repeat: 'loop',
	downbeat_offset: 0,
	level: 80
};
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
	settings: { ...SETTINGS },
	flags: { broken: false, remix: false, vocals: false, offline: false },
	peaks: { low: [0.5], mid: [0.5], high: [0.5] },
	stems_ready: false,
	stem_parts: null
});

/**
 * A fetch that answers like the sampler routes from in-memory state: GET /slots (paged), /sets,
 * /pins, and the PUT /pins/{id}, PUT /sets/{id} and PATCH /slots/{id} writes. Records each call;
 * `failWrites` makes every write answer 500, `status` makes every call answer it.
 */
function samplerServer(catalogue, { pins = [], metas = [], status = 200 } = {}) {
	const state = { pins: [...pins], metas: [...metas], failWrites: false };
	const calls = [];
	const urls = [];
	const json = (body) => new Response(JSON.stringify(body), { status: 200 });
	const fetchFn = async (url, init = {}) => {
		const method = init.method ?? 'GET';
		const body = init.body === undefined ? undefined : JSON.parse(init.body);
		urls.push(String(url));
		calls.push({ method, path: new URL(String(url)).pathname, body });
		const u = new URL(String(url));
		if (status !== 200) return new Response('boom', { status });
		if (method !== 'GET' && state.failWrites) return new Response('write refused', { status: 500 });
		const id = decodeURIComponent(u.pathname.split('/').pop());
		if (method === 'GET' && u.pathname.endsWith('/slots')) {
			const offset = Number(u.searchParams.get('offset'));
			const limit = Number(u.searchParams.get('limit'));
			const slots = catalogue.slice(offset, offset + limit);
			return json({ slots, total: catalogue.length, skipped_no_grid: 0, rekordbox: 'ok' });
		} else if (method === 'GET' && u.pathname.endsWith('/sets')) return json({ sets: state.metas });
		else if (method === 'GET' && u.pathname.endsWith('/pins')) return json({ pins: state.pins });
		else if (method === 'PUT' && u.pathname.includes('/pins/')) {
			state.pins = state.pins.filter((x) => x !== id).concat(body.pinned ? [id] : []);
			return json({ pins: state.pins });
		} else if (method === 'PUT' && u.pathname.includes('/sets/')) {
			const saved = { id, ...body };
			state.metas = [...state.metas.filter((m) => m.id !== id), saved];
			return json(saved);
		} else if (method === 'PATCH' && u.pathname.includes('/slots/')) {
			const old = catalogue.find((x) => x.slot_id === id);
			return json({ ...old, ...body });
		}
		return new Response('no route', { status: 404 });
	};
	return { state, calls, urls, fetchFn };
}

async function loadedStore(catalogue, opts) {
	const S = await loadRuneModule(ENTRY);
	const server = samplerServer(catalogue, opts);
	S.setSamplerFetch(server.fetchFn);
	await S.loadSampler();
	return { S, server, urls: server.urls };
}

test('a 2,000 slot catalogue loads every page and each edge holds one set', async () => {
	/** [if] the server holds 2,000 slots [then] 4 compat pages load, 100 sets of 20, each edge shows 20, [else stop]. */
	const catalogue = Array.from({ length: 2000 }, (_, i) => slot(i));
	const { S, urls } = await loadedStore(catalogue, { pins: ['s3'] });
	const slotCalls = urls.filter((u) => new URL(u).pathname.endsWith('/slots'));
	assert.equal(slotCalls.length, 4);
	assert.ok(slotCalls.every((u) => new URL(u).searchParams.get('sort') === 'compat'));
	assert.equal(S.sampler.loaded, true);
	assert.equal(S.sampler.sets.length, 100);
	assert.equal(S.edgeItems('R').length, 20);
	assert.equal(S.edgeItems('L').length, 20);
	assert.equal(S.edgeItems('R')[0].slot_id, 's0');
	assert.equal(S.edgeItems('L')[0].slot_id, 's20');
	assert.equal(S.isPinned('s3'), true);
	assert.equal(S.sampler.rekordbox, 'ok');
	assert.equal(S.edgeTitle('L').page, 'set 2 / 100');
});

test('refs on screen resolve to the slot the tab shows', async () => {
	/** [if] 45 slots load [then] the L edge's third tab is S2-03 and resolves to s22, a missing set is null, [else stop]. */
	const { S } = await loadedStore(Array.from({ length: 45 }, (_, i) => slot(i)));
	assert.equal(S.edgeRef('L', 2), 'S2-03');
	assert.equal(S.resolveRef('S2-03').slot_id, 's22');
	assert.equal(S.resolveRef('S3-05').slot_id, 's44');
	assert.equal(S.resolveRef('S3-06'), null);
	assert.equal(S.resolveRef('S9-01'), null);
});

test('a failed load toasts, rethrows and leaves the store unloaded', async () => {
	/** [if] GET /slots answers 500 [then] loadSampler rejects, an error toast names it, loaded stays false, [else stop]. */
	const S = await loadRuneModule(ENTRY);
	S.setSamplerFetch(samplerServer([], { status: 500 }).fetchFn);
	await assert.rejects(S.loadSampler(), /HTTP 500/);
	assert.equal(S.sampler.loaded, false);
	assert.ok(S.toasts.some((t) => t.kind === 'error' && /could not load samples/.test(t.message)));
});

test('paging one edge leaves the other edge on its set', async () => {
	/** [if] 45 slots load and R pages forward then back past the start [then] R wraps, L stays on set 2, [else stop]. */
	const { S } = await loadedStore(Array.from({ length: 45 }, (_, i) => slot(i)));
	S.flipSet('R', 1);
	assert.equal(S.sampler.edges.R.set, 1);
	assert.equal(S.sampler.edges.L.set, 1);
	S.flipSet('R', -2);
	assert.equal(S.sampler.edges.R.set, 2);
	assert.equal(S.edgeItems('R')[0].slot_id, 's40');
	assert.equal(S.sampler.lastEdge, 'R');
	assert.ok(S.sampler.edges.R.flipAt > 0);
	assert.equal(S.sampler.edges.L.flipAt, -1e9);
	assert.throws(() => S.setEdgeSet('L', 3), /out of range/);
});

test('a pin saves through the API and a refused pin rolls back', async () => {
	/** [if] a pin saves [then] PUT /pins/s4 carries pinned:true; [if] the next is refused [then] it rolls back with a toast, [else stop]. */
	const { S, server } = await loadedStore(Array.from({ length: 5 }, (_, i) => slot(i)));
	await S.pinSlot('s4', true);
	assert.deepEqual(server.calls.at(-1), {
		method: 'PUT',
		path: '/api/v1/sampler/pins/s4',
		body: { pinned: true }
	});
	assert.equal(S.isPinned('s4'), true);
	server.state.failWrites = true;
	await assert.rejects(S.pinSlot('s2', true), /HTTP 500/);
	assert.deepEqual(S.sampler.pinned, ['s4']);
	assert.ok(S.toasts.some((t) => t.kind === 'error' && /Pin not saved/.test(t.message)));
});

test('a refused slot edit puts the old name back', async () => {
	/** [if] a rename saves [then] the slot reads the new name; [if] the next PATCH is refused [then] it rolls back, [else stop]. */
	const { S, server } = await loadedStore(Array.from({ length: 5 }, (_, i) => slot(i)));
	await S.updateSlot('s1', { name: 'Big stab' });
	assert.equal(S.edgeItems('R')[1].name, 'Big stab');
	server.state.failWrites = true;
	await assert.rejects(S.updateSlot('s1', { name: 'Lost' }), /HTTP 500/);
	assert.equal(S.edgeItems('R')[1].name, 'Big stab');
	assert.ok(S.toasts.some((t) => /Sample not saved, change undone/.test(t.message)));
});

test('a renamed set keeps its name and description across a reload', async () => {
	/** [if] set-2 is named with a description [then] PUT /sets/set-2 saves it and a reload still titles the L edge, [else stop]. */
	const { S, server } = await loadedStore(Array.from({ length: 45 }, (_, i) => slot(i)));
	await S.nameSet('set-2', ' Warmup ', ' peak time ');
	assert.deepEqual(server.calls.at(-1).body, {
		name: 'Warmup',
		description: 'peak time',
		slot_ids: null
	});
	await S.loadSampler();
	assert.deepEqual(S.edgeTitle('L'), {
		name: 'Warmup',
		page: 'set 2 / 3',
		description: 'peak time'
	});
	await assert.rejects(S.nameSet('set-2', '  ', ''), /needs a name/);
});

test('with no engine a fired tab stays not-playing and says why', async () => {
	/** [if] a tab fires before any engine registers [then] its phase stays free and an error toast shows, [else stop]. */
	const { S } = await loadedStore(Array.from({ length: 5 }, (_, i) => slot(i)));
	assert.equal(S.sampler.engineReady, false);
	await S.fireSlot(S.edgeItems('R')[0]);
	assert.equal(S.slotPhase('s0'), 'free');
	assert.ok(S.toasts.some((t) => t.kind === 'error' && /no audio engine yet/.test(t.message)));
	await S.togglePreview(S.edgeItems('R')[1], false);
	assert.equal(S.slotPhase('s1'), 'free');
});

test('a registered engine receives the plays and warms the visible tabs', async () => {
	/** [if] an engine registers [then] engineReady is true, a fired tab reaches its toggle, a reload warms the shown tabs, [else stop]. */
	const { S } = await loadedStore(Array.from({ length: 25 }, (_, i) => slot(i)));
	const calls = [];
	S.registerSamplerEngine({
		toggle: async (s) => void calls.push(['toggle', s.slot_id]),
		stop: (id) => void calls.push(['stop', id]),
		preview: async (s) => void calls.push(['preview', s.slot_id]),
		stopPreview: () => void calls.push(['stopPreview']),
		warm: (slots) => void calls.push(['warm', slots.length]),
		phase: () => 'playing'
	});
	assert.equal(S.sampler.engineReady, true);
	S.fireEdgeTab('R', 2);
	await S.loadSampler();
	assert.deepEqual(calls, [
		['toggle', 's2'],
		['warm', 25]
	]);
	assert.equal(S.slotPhase('s2'), 'playing');
});
