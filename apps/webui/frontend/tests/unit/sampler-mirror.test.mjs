/**
 * Issue #10786 (S9): the ui-mirror's `sampler` section tells an agent what the edges show and
 * which slots sound, without reading the UI.
 *
 * Regression lines:
 * - if an edge shows search results then the mirror carries their count, else null
 * - if one slot plays and one previews then voices has one row and preview names the other
 * - if the section aliases the store's arrays then a later store write rewrites a published mirror
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
before(async () => {
	m = await loadTypeScriptModule('src/lib/player/sampler/sampler-mirror.ts');
});

const FILTERS = { broken: false, remixes: false, vocals: true, offline: false, compatible: false };
const edge = (over = {}) => ({
	set: 0,
	query: '',
	filters: { ...FILTERS },
	results: null,
	rpage: 0,
	awake: false,
	...over
});
const source = () => ({
	slots: [{ slot_id: 's1' }, { slot_id: 's2' }, { slot_id: 's3' }, { slot_id: 's4' }],
	edges: {
		L: edge({ set: 1 }),
		R: edge({ query: 'horn', results: [{}, {}, {}], rpage: 1, awake: true })
	},
	pinned: ['s2'],
	editing: 's3',
	loaded: true,
	engineReady: true
});
const PHASES = { s1: 'playing', s2: 'free', s3: 'previewing', s4: 'armed' };

test('the mirror reads each edge, the pins, the editor and the sounding slots', () => {
	/** [if] R shows 3 results and s1 plays, s4 is armed, s3 previews [then] the section says exactly that, [else stop]. */
	const out = m.buildSamplerMirror(source(), true, (id) => PHASES[id]);
	assert.deepEqual(out, {
		enabled: true,
		loaded: true,
		engine_ready: true,
		edges: {
			L: { set: 1, results: null, rpage: 0, query: '', filters: FILTERS, awake: false },
			R: { set: 0, results: 3, rpage: 1, query: 'horn', filters: FILTERS, awake: true }
		},
		pinned: ['s2'],
		editing: 's3',
		voices: [
			{ slot_id: 's1', phase: 'playing' },
			{ slot_id: 's4', phase: 'armed' }
		],
		preview: 's3'
	});
});

test('with nothing sounding and the sampler off the section says so', () => {
	/** [if] every slot is free and the sampler is off [then] voices is empty, preview null, enabled false, [else stop]. */
	const out = m.buildSamplerMirror(source(), false, () => 'free');
	assert.equal(out.enabled, false);
	assert.deepEqual(out.voices, []);
	assert.equal(out.preview, null);
});

test('the section is a copy, not a view of the store', () => {
	/** [if] a later store write changes a published section [then] the mirror leaks live state, [else stop]. */
	const src = source();
	const out = m.buildSamplerMirror(src, true, () => 'free');
	src.pinned.push('s9');
	src.edges.L.filters.vocals = false;
	assert.deepEqual(out.pinned, ['s2']);
	assert.equal(out.edges.L.filters.vocals, true);
	assert.deepEqual(JSON.parse(JSON.stringify(out)), out);
});
