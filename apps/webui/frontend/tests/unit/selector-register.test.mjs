/**
 * K5 / #9397: selector node registration core (selector-register.ts).
 *
 * Regression lines:
 * - if resolveSelectorNodes keeps a disabled node or any of its descendants then broken
 * - if resolveSelectorNodes accepts a parentId that names no record then broken
 * - if root sections are not ordered topbar, waveforms, decks, library by reading order then broken
 * - if unionRect counts zero-area rects or returns a non-bounding box then broken
 * - if selId builders do not produce deck.1.header and mixer.ch4.low then broken
 * - if drilling decks once then twice does not give deck.1 then deck.1.header then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { resolveSelectorNodes, selId, unionRect } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-register.ts'
);

const rec = (id, parentId, x, y, w, h, kind = 'leaf', enabled = true) => ({
	id,
	parentId,
	kind,
	enabled,
	rect: { x, y, width: w, height: h }
});

/** Laid out like /performance in four-deck mode, records in arbitrary order. */
function performanceFixture({ lessMode = false } = {}) {
	const deck = (n, x, y) => [
		rec(`deck.${n}`, 'decks', x, y, 500, 250, 'group', !(lessMode && n > 2)),
		rec(`deck.${n}.header`, `deck.${n}`, x, y + 20, 500, 75),
		rec(`deck.${n}.cues`, `deck.${n}`, x, y + 120, 200, 100),
		rec(`deck.${n}.jog`, `deck.${n}`, x + 300, y + 120, 100, 100)
	];
	return [
		rec('library', null, 0, 800, 1400, 300, 'section'),
		...deck(3, 0, 550),
		rec('decks', null, 0, 300, 1400, 500, 'section'),
		...deck(1, 0, 300),
		rec('mixer', 'decks', 500, 300, 400, 500, 'group'),
		rec('mixer.ch4', 'mixer', 800, 320, 100, 400, 'group', !lessMode),
		rec('mixer.ch4.low', 'mixer.ch4', 800, 400, 100, 40),
		...deck(2, 900, 300),
		...deck(4, 900, 550),
		rec('waveforms', null, 0, 40, 1400, 260, 'section'),
		rec('topbar', null, 0, 0, 1400, 40, 'section')
	];
}

test('root sections come first in reading order', () => {
	const nodes = resolveSelectorNodes(performanceFixture());
	const roots = nodes.filter((n) => n.parentId === null).map((n) => n.id);
	assert.deepEqual(roots, ['topbar', 'waveforms', 'decks', 'library']);
	assert.deepEqual(
		nodes.slice(0, 4).map((n) => n.id),
		roots
	);
	assert.ok(!('enabled' in nodes[0]), 'resolved nodes carry only SelectorNode fields');
});

test('disabled node and its subtree are dropped', () => {
	const ids = resolveSelectorNodes(performanceFixture({ lessMode: true })).map((n) => n.id);
	for (const gone of ['deck.3', 'deck.3.header', 'deck.4.jog', 'mixer.ch4', 'mixer.ch4.low']) {
		assert.ok(!ids.includes(gone), `${gone} should be dropped`);
	}
	assert.ok(ids.includes('deck.1.header'));
});

test('unknown parentId and duplicate ids throw', () => {
	assert.throws(
		() => resolveSelectorNodes([rec('a', 'nope', 0, 0, 1, 1)]),
		/Unknown parentId.*nope/
	);
	assert.throws(
		() => resolveSelectorNodes([rec('a', null, 0, 0, 1, 1), rec('a', null, 0, 0, 1, 1)]),
		/Duplicate selector node id: a/
	);
});

test('unionRect skips zero-area rects and bounds the rest', () => {
	assert.deepEqual(
		unionRect([
			{ x: 10, y: 10, width: 10, height: 10 },
			{ x: 0, y: 0, width: 0, height: 0 },
			{ x: 30, y: 5, width: 5, height: 40 }
		]),
		{ x: 10, y: 5, width: 25, height: 40 }
	);
	assert.deepEqual(unionRect([{ x: 5, y: 5, width: 0, height: 9 }]), {
		x: 0,
		y: 0,
		width: 0,
		height: 0
	});
});

test('selId builders give stable ids', () => {
	assert.equal(selId.deckPart(1, 'header'), 'deck.1.header');
	assert.equal(selId.channelPart(4, 'low'), 'mixer.ch4.low');
	assert.equal(selId.channelStem(2, 'vocals'), 'mixer.ch2.stem.vocals');
	assert.equal(selId.section('library'), 'library');
	assert.equal(selId.crossfader(), 'mixer.crossfader');
});

test('drilling decks lands on deck.1 then deck.1.header', () => {
	const nodes = resolveSelectorNodes(performanceFixture());
	const tree = createSelectorTree(nodes, nodes[0].id);
	assert.equal(tree.selected(), 'topbar');
	assert.equal(tree.move('down'), 'waveforms');
	assert.equal(tree.move('down'), 'decks');
	assert.equal(tree.drillIn(), 'deck.1');
	assert.equal(tree.drillIn(), 'deck.1.header');
});
