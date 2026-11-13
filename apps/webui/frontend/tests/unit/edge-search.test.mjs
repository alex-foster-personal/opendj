// requirement: EDGESAMP-13997
/**
 * Issue #11102 (S7b): the top tab's search and filters, pure parts ((private path) 7.4).
 * Matching, highlight ranges, the counts line, and how search moves tab widths on the wave.
 *
 * Regression lines:
 * - if the search box has focus then every tab of that edge is fully out
 * - if a tab does not match then it collapses to its numbered strip, never to nothing
 * - if a query is typed then only on-screen slots are counted here and all sets come from the server total
 * - if a broken track is listed and the Broken filter is off then it is hidden
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let search;
let wave;
before(async () => {
	search = await loadTypeScriptModule('src/lib/player/sampler/edge-search.ts');
	wave = await loadTypeScriptModule('src/lib/player/sampler/edge-wave.ts');
});

const slot = (o = {}) => ({
	title: 'Low Tide',
	name: null,
	cueLabel: 'A 4 bars',
	description: '',
	key: '8A',
	broken: false,
	remix: false,
	vocals: false,
	offline: true,
	compatible: true,
	...o
});

const tab = (o = {}) => ({
	awake: true,
	dy: 2000,
	tabH: 30,
	held: false,
	filled: true,
	ramp: 1,
	...o
});

test('search focus pops every tab of the edge', () => {
	/** [if] the edge's search box has focus [then] a far-off filled tab is at pop width, an empty one at its cap, [else stop]. */
	assert.equal(
		wave.tabTargetWidth(tab({ searchOpen: true }), wave.DEFAULT_WAVE),
		wave.DEFAULT_WAVE.pop
	);
	assert.equal(
		wave.tabTargetWidth(tab({ searchOpen: true, filled: false }), wave.DEFAULT_WAVE),
		wave.EMPTY_POP_W
	);
	assert.ok(wave.tabTargetWidth(tab(), wave.DEFAULT_WAVE) < wave.AWAKE_W + 1);
});

test('a non-matching tab collapses to its numbered strip', () => {
	/** [if] a tab does not match, even under the pointer or held [then] it is the 32 px numbered strip, not 0, [else stop]. */
	const off = { searchOpen: true, matches: false };
	assert.equal(wave.tabTargetWidth(tab({ ...off, dy: 0 }), wave.DEFAULT_WAVE), wave.AWAKE_W);
	assert.equal(wave.tabTargetWidth(tab({ ...off, held: true }), wave.DEFAULT_WAVE), wave.AWAKE_W);
	assert.equal(search.matchesSlot(slot(), 'riser', search.NO_FILTERS), false);
});

test('another tab in the edit modal settles this one to the strip', () => {
	/** [if] another tab of the edge is being edited [then] this tab under the pointer is the awake strip, [else stop]. */
	assert.equal(
		wave.tabTargetWidth(tab({ dy: 0, editingOther: true }), wave.DEFAULT_WAVE),
		wave.AWAKE_W
	);
});

test('the counts line scans only on-screen slots and takes the all-sets total as given', () => {
	/** [if] 2 of the on-screen slots match and the server total is 9 [then] it reads "2 here . 9 in all sets", [else stop]. */
	const onScreen = [slot(), slot({ title: 'Big Riser' }), null, slot({ name: 'tide pool' })];
	const here = search.countHere(onScreen, 'tide', search.NO_FILTERS);
	assert.equal(here, 2);
	assert.equal(search.countsLine(here, 9), '2 here . 9 in all sets');
	assert.equal(search.countsLine(here, null), '2 here');
});

test('matching is case-insensitive over name, title, cue, description and key', () => {
	/** [if] the query differs in case or hits the rename, cue label or key [then] the slot matches, [else stop]. */
	assert.equal(search.matchesSlot(slot(), 'TIDE', search.NO_FILTERS), true);
	assert.equal(search.matchesSlot(slot({ name: 'Big riser' }), 'riser', search.NO_FILTERS), true);
	assert.equal(search.matchesSlot(slot(), '4 bars', search.NO_FILTERS), true);
	assert.equal(search.matchesSlot(slot(), ' 8a ', search.NO_FILTERS), true);
});

test('broken tracks hide unless the Broken filter is on, other toggles narrow', () => {
	/** [if] a slot is broken with no filters, or Remixes is on for a non-remix [then] it is hidden, [else stop]. */
	const f = search.NO_FILTERS;
	assert.equal(search.matchesSlot(slot({ broken: true }), '', f), false);
	assert.equal(search.matchesSlot(slot({ broken: true }), '', { ...f, broken: true }), true);
	assert.equal(search.matchesSlot(slot(), '', { ...f, remixes: true }), false);
	assert.equal(
		search.matchesSlot(slot({ compatible: false }), '', { ...f, compatible: true }),
		false
	);
});

test('filtering means a query or any toggle on', () => {
	/** [if] the query is blank and no toggle is on [then] the edge is not filtering, else it is, [else stop]. */
	assert.equal(search.isFiltering('  ', search.NO_FILTERS), false);
	assert.equal(search.isFiltering('x', search.NO_FILTERS), true);
	assert.equal(search.isFiltering('', { ...search.NO_FILTERS, vocals: true }), true);
});

test('highlight ranges cover every occurrence without overlap', () => {
	/** [if] "aa" is highlighted in "aaaa" or "TIDE" in "Low Tide" [then] ranges are [0,2],[2,4] and [4,8], [else stop]. */
	assert.deepEqual(search.highlightRanges('aaaa', 'aa'), [
		[0, 2],
		[2, 4]
	]);
	assert.deepEqual(search.highlightRanges('Low Tide', 'TIDE'), [[4, 8]]);
	assert.deepEqual(search.highlightRanges('Low Tide', '  '), []);
});
