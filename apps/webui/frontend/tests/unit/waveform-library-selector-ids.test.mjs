// requirement: KEYB-13918
/**
 * #13703: waveform lane and library pane selector nodes.
 *
 * Regression lines:
 * - if drilling into the waveforms section does not list one leaf per deck lane, top to bottom, then broken
 * - if drilling into the library section does not list tree, search and tracks as leaves then broken
 * - if two-deck mode leaves lanes 3 or 4 live in the selector tree then broken
 * - if the waveform area's sampler chip steals the drill-in or a lane move down from the lanes then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { libraryPaneSelectorId, waveLaneSelectable, waveLaneSelectorId, waveSamplerSelectorId } =
	await loadTypeScriptModule('src/lib/components/rb/waveform-library-selector-ids.ts');
const { resolveSelectorNodes, selId } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-register.ts'
);

const W = 1440;
const LANE_H = 40;
const DECKS = [1, 2, 3, 4];

const rec = (id, parentId, x, y, w, h, kind = 'leaf', enabled = true) => ({
	id,
	parentId,
	kind,
	enabled,
	rect: { x, y, width: w, height: h }
});

/** The four page sections, the four wave lanes in grid order and the library's three panes. */
function pageFixture(layout = 'more') {
	const out = [
		rec(selId.section('topbar'), null, 0, 0, W, 28, 'section'),
		rec(selId.section('waveforms'), null, 0, 28, W, 4 * LANE_H, 'section'),
		rec(selId.section('decks'), null, 0, 190, W, 500, 'section'),
		rec(selId.section('library'), null, 0, 700, W, 300, 'section'),
		rec(libraryPaneSelectorId('tree'), selId.section('library'), 0, 700, 220, 300),
		rec(libraryPaneSelectorId('search'), selId.section('library'), 1180, 704, 250, 24),
		rec(libraryPaneSelectorId('tracks'), selId.section('library'), 224, 734, W - 224, 266)
	];
	for (const deck of DECKS) {
		const hidden = !waveLaneSelectable(deck, layout);
		const y = 28 + (deck - 1) * LANE_H;
		out.push(
			rec(
				waveLaneSelectorId(deck),
				selId.section('waveforms'),
				0,
				y,
				W,
				hidden ? 0 : LANE_H,
				'leaf',
				!hidden
			)
		);
	}
	return out;
}

const childIds = (nodes, parentId) =>
	nodes.filter((node) => node.parentId === parentId).map((node) => node.id);
const kindOf = (nodes, id) => nodes.find((node) => node.id === id)?.kind;

/** [if] the waveforms section is drilled into [then] each visible deck lane is a leaf in top to bottom order, [else stop]. */
test('KEYB #13703: waveform lanes are leaves in top to bottom order', () => {
	assert.equal(waveLaneSelectorId(3), 'waveforms.deck3');
	const nodes = resolveSelectorNodes(pageFixture());
	const lanes = childIds(nodes, 'waveforms');
	assert.deepEqual(
		lanes.sort(),
		DECKS.map((d) => `waveforms.deck${d}`)
	);
	for (const id of lanes) assert.equal(kindOf(nodes, id), 'leaf', id);
	const tree = createSelectorTree(nodes, 'waveforms');
	assert.equal(tree.drillIn(), 'waveforms.deck1');
	assert.deepEqual(
		[tree.move('down'), tree.move('down'), tree.move('down'), tree.move('down')],
		['waveforms.deck2', 'waveforms.deck3', 'waveforms.deck4', 'waveforms.deck4']
	);
	assert.equal(tree.drillOut(), 'waveforms');
	const sections = createSelectorTree(nodes, 'topbar');
	assert.equal(sections.move('down'), 'waveforms', 'lanes never take a section move');
	assert.equal(sections.move('down'), 'decks');
});

/** [if] the library section is drilled into [then] tree, search and tracks are its leaves and reachable by moves, [else stop]. */
test('KEYB #13703: library tree, search and tracks are leaves', () => {
	assert.equal(libraryPaneSelectorId('tracks'), 'library.tracks');
	const nodes = resolveSelectorNodes(pageFixture());
	const panes = childIds(nodes, 'library');
	assert.deepEqual(panes.sort(), ['library.search', 'library.tracks', 'library.tree']);
	for (const id of panes) assert.equal(kindOf(nodes, id), 'leaf', id);
	const tree = createSelectorTree(nodes, 'library');
	assert.equal(tree.drillIn(), 'library.tree');
	assert.equal(tree.move('right'), 'library.tracks');
	assert.equal(tree.move('up'), 'library.search');
	assert.equal(tree.move('down'), 'library.tracks');
	assert.equal(tree.move('left'), 'library.tree');
	const sections = createSelectorTree(nodes, 'decks');
	assert.equal(sections.move('down'), 'library', 'panes never take a section move');
});

/** [if] two-deck mode hides lanes 3 and 4 [then] those leaves are not live and a move down stops at lane 2, [else stop]. */
test('KEYB #13703: two-deck mode drops lanes 3 and 4', () => {
	assert.equal(waveLaneSelectable(3, 'less'), false);
	assert.equal(waveLaneSelectable(4, 'less'), false);
	assert.equal(waveLaneSelectable(2, 'less'), true);
	assert.equal(waveLaneSelectable(4, 'more'), true);
	const nodes = resolveSelectorNodes(pageFixture('less'));
	assert.deepEqual(childIds(nodes, 'waveforms').sort(), ['waveforms.deck1', 'waveforms.deck2']);
	const tree = createSelectorTree(nodes, 'waveforms');
	assert.equal(tree.drillIn(), 'waveforms.deck1');
	assert.equal(tree.move('down'), 'waveforms.deck2');
	assert.equal(tree.move('down'), 'waveforms.deck2');
});

/** WaveformStack.svelte `.sampler-toggle-slot`: absolute at top 2px, right 66px; the chip is 40 x 22. */
const SAMPLER_W = 40;
const SAMPLER_H = 22;
const SAMPLER_TOP = 2;
const SAMPLER_RIGHT = 66;

/** The waveforms section alone: lanes of `laneH` stacked from y 28, plus the sampler chip. */
function waveStackFixture(layout, laneH) {
	const top = 28;
	const out = [rec(selId.section('waveforms'), null, 0, top, W, 4 * laneH, 'section')];
	for (const deck of DECKS) {
		const hidden = !waveLaneSelectable(deck, layout);
		const y = top + (deck - 1) * laneH;
		out.push(
			rec(waveLaneSelectorId(deck), 'waveforms', 0, y, W, hidden ? 0 : laneH, 'leaf', !hidden)
		);
	}
	const chipX = W - SAMPLER_RIGHT - SAMPLER_W;
	out.push(
		rec(waveSamplerSelectorId(), 'waveforms', chipX, top + SAMPLER_TOP, SAMPLER_W, SAMPLER_H)
	);
	return out;
}

/** [if] the sampler chip sits in the waveform stack [then] it is leaf waveforms.sampler and lanes keep drill-in and down, [else stop]. */
test('KEYB #13703: the sampler chip is a waveforms leaf that never takes a lane move', () => {
	assert.equal(waveSamplerSelectorId(), 'waveforms.sampler');
	const chip = readFileSync('src/lib/components/rb/sampler/EdgeSamplerToggle.svelte', 'utf8');
	assert.match(chip, /use:areaTrackedSelectorLeaf=\{\{ id: waveSamplerSelectorId\(\)/);
	// 43px default, 56px skins, 22px compact (max-height 799px), 20px smallest skin.
	for (const laneH of [43, 56, 22, 20]) {
		const nodes = resolveSelectorNodes(waveStackFixture('more', laneH));
		assert.deepEqual(childIds(nodes, 'waveforms').sort(), [
			...DECKS.map((d) => `waveforms.deck${d}`),
			'waveforms.sampler'
		]);
		assert.equal(kindOf(nodes, 'waveforms.sampler'), 'leaf');
		const tree = createSelectorTree(nodes, 'waveforms');
		assert.equal(tree.drillIn(), 'waveforms.deck1', `drill-in at ${laneH}px`);
		assert.deepEqual(
			[tree.move('down'), tree.move('down'), tree.move('down'), tree.move('down')],
			['waveforms.deck2', 'waveforms.deck3', 'waveforms.deck4', 'waveforms.deck4'],
			`down walk at ${laneH}px`
		);
		const less = resolveSelectorNodes(waveStackFixture('less', laneH));
		assert.deepEqual(childIds(less, 'waveforms').sort(), [
			'waveforms.deck1',
			'waveforms.deck2',
			'waveforms.sampler'
		]);
		const two = createSelectorTree(less, 'waveforms');
		assert.equal(two.drillIn(), 'waveforms.deck1', `LESS drill-in at ${laneH}px`);
		assert.equal(two.move('down'), 'waveforms.deck2');
		assert.equal(two.move('down'), 'waveforms.deck2', `LESS down from deck 2 at ${laneH}px`);
	}
});
