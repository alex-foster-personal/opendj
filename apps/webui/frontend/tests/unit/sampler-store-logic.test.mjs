// requirement: EDGESAMP-14011
/**
 * Issue #11102 (S7/S7b): the edge sampler store's pure parts ((private path) 7.4, 10).
 * Refs, the compat sort hand-off, set layout, optimistic patches, pins and compatibility.
 *
 * Regression lines:
 * - if a ref reads S2-03 then it is the third slot of the second set, and a missing one is null
 * - if a query is typed then the all-sets search goes to the server as q, master fields only when known
 * - if set-1 has a saved layout then it keeps it and set-2 starts at the first unplaced slot
 * - if a slot patch fails then the slot rolls back and the error reaches the caller
 * - if the library's compatible prefs change then the sampler's verdict follows the same object
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let L;
let next;
let prefsMod;
before(async () => {
	L = await loadTypeScriptModule('src/lib/player/sampler/sampler-store-logic.ts');
	next = await loadTypeScriptModule('src/lib/rb/next-only-filter.ts');
	prefsMod = await loadTypeScriptModule('src/lib/rb/compatible-filter-prefs.ts');
});

const SETTINGS = {
	launch: 'bar',
	key: 'auto',
	tempo: 'match',
	mode: 'loop',
	repeat: 'loop',
	downbeat_offset: 0,
	level: 80
};
const slot = (i, over = {}) => ({
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
	flags: { broken: false, remix: false, vocals: false, offline: true },
	peaks: { low: [0.5], mid: [0.5], high: [0.5] },
	stems_ready: false,
	stem_parts: null,
	...over
});
const catalogue = Array.from({ length: 45 }, (_, i) => slot(i));
const meta = (id, slot_ids, name = '') => ({ id, name, description: '', slot_ids });

test('a set ref resolves to its slot on screen', () => {
	/** [if] the ref is S2-03 on a 45-slot catalogue [then] it parses to set 1 index 2 and resolves to s22, [else stop]. */
	assert.deepEqual(L.parseRef('S2-03'), { kind: 'set', set: 1, index: 2 });
	assert.equal(L.resolveRefIn('S2-03', L.layoutSets(catalogue, 20, []), null).slot_id, 's22');
});

test('a results ref reads the search results, or null without them', () => {
	/** [if] the ref is R-01 with and without search results [then] it is the first result, else null, [else stop]. */
	assert.equal(L.resolveRefIn('R-01', [], [slot(7)]).slot_id, 's7');
	assert.equal(L.resolveRefIn('R-01', [], null), null);
});

test('a ref to a missing set, an empty slot or a malformed ref is null', () => {
	/** [if] the ref names set 9, an empty slot, slot 00 or is junk [then] it resolves to null, [else stop]. */
	const sets = L.layoutSets(catalogue, 20, [meta('set-1', ['s1', null], 'Mine')]);
	for (const ref of ['S9-01', 'S1-02', 'S1-00', 'S0-01', 'banana'])
		assert.equal(L.resolveRefIn(ref, sets, null), null, ref);
});

test('a typed query asks the server for all sets, with master fields only when known', () => {
	/** [if] "tide" is typed with a 124.004 BPM 8A master, then with none [then] q is sent and master fields only once, [else stop]. */
	assert.deepEqual(L.slotsQueryFor({ key: '8A', bpm: 124.004 }, 0, 500, 'tide'), {
		sort: 'compat',
		q: 'tide',
		offset: 0,
		limit: 500,
		master_bpm: 124,
		master_key: '8A'
	});
	const q = L.slotsQueryFor(null, 0, 500);
	assert.equal(q.q, '');
	assert.equal('master_bpm' in q || 'master_key' in q, false);
});

test('a new master re-ranks at most every 5 s', () => {
	/** [if] the master changes 2 s after the last rank [then] it waits 3 s, and a tiny BPM drift is the same master, [else stop]. */
	assert.equal(L.rerankDelay(10_000, 12_000), 3_000);
	assert.equal(L.rerankDelay(10_000, 20_000), 0);
	assert.equal(L.sameMaster({ key: '8A', bpm: 124 }, { key: '8A', bpm: 124.01 }), true);
	assert.equal(L.sameMaster({ key: '8A', bpm: 124 }, { key: '9A', bpm: 124 }), false);
	assert.equal(L.sameMaster(null, null), true);
});

test('sets without a saved layout are pages of the ranked catalogue', () => {
	/** [if] no set has slot_ids and set-2 has a name [then] sets are 20, 20, 5 and set-2 keeps its name, [else stop]. */
	const sets = L.layoutSets(catalogue, 20, [
		{ id: 'set-2', name: 'Named', description: 'd', slot_ids: null }
	]);
	assert.deepEqual(
		sets.map((s) => s.items.length),
		[20, 20, 5]
	);
	assert.deepEqual(
		sets.map((s) => s.name),
		['Set 1', 'Named', 'Set 3']
	);
	assert.throws(() => L.layoutSets(catalogue, 0, []), /perSet must be positive/);
});

test('a saved layout keeps its slots and the next set starts at the first unplaced one', () => {
	/** [if] set-1 saves s40, empty, s0 and a gone id [then] it shows those and set-2 starts at s1, s40 once, [else stop]. */
	const sets = L.layoutSets(catalogue, 20, [meta('set-1', ['s40', null, 's0', 'gone'], 'Mine')]);
	assert.deepEqual(L.setSlotIds(sets[0]), ['s40', null, 's0', null]);
	assert.equal(sets[1].items[0].slot_id, 's1');
	assert.equal(sets.flatMap((s) => L.setSlotIds(s)).filter((id) => id === 's40').length, 1);
});

test('starter slots never land in a user set', () => {
	/** [if] the catalogue has 3 own slots and 2 starter slots [then] one set of the 3 own slots is laid out, [else stop]. */
	const mixed = [
		...catalogue.slice(0, 3),
		slot('st0', { source: 'starter' }),
		slot('st1', { source: 'starter' })
	];
	assert.deepEqual(L.layoutSets(mixed, 20, []).map(L.setSlotIds), [['s0', 's1', 's2']]);
});

test('a drop that shifts a full set reports every changed set', () => {
	/** [if] s44 is dropped at the top of set-1 [then] set-1, set-2 and set-3 all differ and need saving, [else stop]. */
	const before = L.layoutSets(catalogue, 20, []);
	const after = L.layoutSets(catalogue, 20, [
		meta('set-1', ['s44', ...L.setSlotIds(before[0]).slice(0, 19)], 'Set 1')
	]);
	assert.deepEqual(
		L.changedSets(before, after).map((s) => s.id),
		['set-1', 'set-2', 'set-3']
	);
	assert.deepEqual(L.changedSets(before, before), []);
});

test('a failed slot patch rolls back and rethrows', async () => {
	/** [if] the PATCH rejects after a rename [then] the slot shows New, then null again, and the error reaches the caller, [else stop]. */
	const store = new Map([['s1', slot(1)]]);
	const seen = [];
	const put = (s) => {
		store.set(s.slot_id, s);
		seen.push(s.name);
	};
	const failing = async () => {
		throw new Error('HTTP 500');
	};
	await assert.rejects(
		L.patchWithRollback((id) => store.get(id) ?? null, put, 's1', { name: 'New' }, failing),
		/HTTP 500/
	);
	assert.deepEqual(seen, ['New', null]);
	await assert.rejects(
		L.patchWithRollback(() => null, put, 'nope', {}, failing),
		/no slot nope/
	);
});

test('a successful slot patch keeps the server copy', async () => {
	/** [if] a repeat patch succeeds and the server answers level 70 [then] the stored slot has level 70, [else stop]. */
	const store = new Map([['s1', slot(1)]]);
	const put = (s) => store.set(s.slot_id, s);
	const settings = { ...SETTINGS, repeat: 4 };
	const saved = await L.patchWithRollback(
		(id) => store.get(id) ?? null,
		put,
		's1',
		{ settings },
		async () => slot(1, { settings: { ...settings, level: 70 } })
	);
	assert.equal(store.get('s1').settings.level, 70);
	assert.equal(saved.settings.repeat, 4);
	const kept = L.applySlotPatch(slot(1, { name: 'Mine' }), { description: 'x' });
	assert.deepEqual([kept.name, kept.description, kept.settings], ['Mine', 'x', SETTINGS]);
});

test('pinning lists a slot once, last, and unpinning removes only it', () => {
	/** [if] a slot is pinned twice or unpinned [then] it is listed once, last, or gone with the rest kept, [else stop]. */
	assert.deepEqual(L.withPin(['a', 'b'], 'a', true), ['b', 'a']);
	assert.deepEqual(L.withPin(['a', 'b'], 'c', true), ['a', 'b', 'c']);
	assert.deepEqual(L.withPin(['a', 'b'], 'a', false), ['b']);
	assert.deepEqual(L.withPin([], 'a', false), []);
});

test('the compatible verdict follows the library prefs object it is given', () => {
	/** [if] the library prefs narrow the BPM window from 20 to 2 [then] a 130 BPM slot on a 124 master flips like the library's, [else stop]. */
	const master = { key: '8A', bpm: 124 };
	const s = slot(1, { native_bpm: 130 });
	const wide = { ...prefsMod.COMPATIBLE_FILTER_DEFAULTS };
	const narrow = { ...wide, bpm_window_bpm: 2 };
	for (const prefs of [wide, narrow])
		assert.equal(
			L.slotIsCompatible(s, master, prefs),
			next.isAppropriateNext({ key: s.key, bpm: s.native_bpm, energy: s.energy }, master, prefs)
		);
	assert.equal(L.slotIsCompatible(s, master, wide), true);
	assert.equal(L.slotIsCompatible(s, master, narrow), false);
	assert.equal(L.slotIsCompatible(s, null, wide), false);
	assert.equal(L.slotIsCompatible(slot(2, { key: '5B' }), master, wide), false);
	assert.equal(L.bpmIsCompatible(slot(2, { key: '5B' }), master, wide), true);
});

test('the key plan names same key, shifted or incompatible', () => {
	/** [if] a slot is 8A, 3B (shift 1 allowed), 5B or keyless on an 8A master [then] dist is 0, 1 at 8B, 2 and 2, [else stop]. */
	const prefs = prefsMod.COMPATIBLE_FILTER_DEFAULTS;
	const master = { key: '8A', bpm: 124 };
	assert.deepEqual(L.keyPlanFor(slot(1), master, prefs), { key: '8A', shift: 0, dist: 0 });
	assert.deepEqual(L.keyPlanFor(slot(2, { key: '3B' }), master, prefs), {
		key: '8B',
		shift: -1,
		dist: 1
	});
	assert.equal(L.keyPlanFor(slot(2, { key: '5B' }), master, prefs).dist, 2);
	assert.deepEqual(L.keyPlanFor(slot(3, { key: null }), master, prefs), {
		key: null,
		shift: 0,
		dist: 2
	});
	assert.equal(L.keyPlanFor(slot(1), null, prefs).dist, 2);
});

test('a slot reads for search with its cue label and flags', () => {
	/** [if] a hot cue A, a capture and a cue-less slot of 1 bar are read [then] labels are A 4 bars, loop 4 bars, 1 bar, [else stop]. */
	const v = L.toSearchable(
		slot(1, { flags: { broken: true, remix: false, vocals: true, offline: false } }),
		true
	);
	assert.equal(v.cueLabel, 'A 4 bars');
	assert.equal(v.broken && v.vocals && v.compatible && !v.offline, true);
	assert.equal(L.cueLabel({ cue_letter: null, beats: 16, source: 'capture' }), 'loop 4 bars');
	assert.equal(L.cueLabel({ cue_letter: null, beats: 4, source: 'memory_loop' }), '1 bar');
	assert.equal(L.toSearchable(slot(1, { key: null }), false).key, '');
});
