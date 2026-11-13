/**
 * requirement: KEYB-13865 (#13698): selector node ids for deck pads, pad mode menu, beat jump and grid edit (deck-pad-selector-ids.ts).
 *
 * Regression lines:
 * - if deck 1 cues does not hold eight pad leaves deck.1.cues.pad1 to pad8 then broken
 * - if deck 1 loop does not hold the beat jump back and forward leaves then broken
 * - if the pad mode menu is not a leaf under deck.<n>.cues then broken
 * - if a disabled pad leaf stays in the resolved tree then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ids = await loadTypeScriptModule('src/lib/components/rb/deck/deck-pad-selector-ids.ts');
const { resolveSelectorNodes } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-register.ts'
);

const rec = (id, parentId, x, y, kind = 'leaf', enabled = true) => ({
	id,
	parentId,
	kind,
	enabled,
	rect: { x, y, width: 20, height: 20 }
});

/** Deck 1 laid out the way Deck.svelte nests it: grid edit and menu left, 2x4 pads, loop column right. */
function deckOneRecords(padEnabled = () => true) {
	const records = [
		rec('decks', null, 0, 0, 'section'),
		rec('deck.1', 'decks', 0, 0, 'group'),
		rec('deck.1.cues', 'deck.1', 0, 0, 'group'),
		rec('deck.1.loop', 'deck.1', 300, 0, 'group'),
		rec(ids.gridEditSelId(1, 'adjust'), 'deck.1.cues', 0, 0),
		rec(ids.gridEditSelId(1, 'shift'), 'deck.1.cues', 0, 30),
		rec(ids.padModeMenuSelId(1), 'deck.1.cues', 0, 60),
		rec(ids.loopClusterSelId(1), 'deck.1.loop', 300, 0),
		rec(ids.beatJumpSelId(1, 'back'), 'deck.1.loop', 360, 60),
		rec(ids.beatJumpSelId(1, 'forward'), 'deck.1.loop', 390, 60)
	];
	for (let k = 1; k <= 8; k++) {
		const column = k <= 4 ? 0 : 1;
		const row = (k - 1) % 4;
		records.push(
			rec(ids.deckPadSelId(1, k), 'deck.1.cues', 40 + column * 100, row * 30, 'leaf', padEnabled(k))
		);
	}
	return records;
}

const childIds = (nodes, parentId) =>
	nodes
		.filter((n) => n.parentId === parentId)
		.map((n) => n.id)
		.sort();

test('KEYB-13865 pads: deck 1 cues holds eight pad leaves deck.1.cues.pad1 to pad8', () => {
	// [if] deck 1 cues is drilled into [then] each of the 8 pads is a leaf deck.1.cues.pad<k>, [else stop].
	const nodes = resolveSelectorNodes(deckOneRecords());
	const pads = nodes.filter((n) => n.parentId === 'deck.1.cues' && n.id.includes('.pad'));
	assert.deepEqual(
		pads.map((n) => n.id).sort(),
		[1, 2, 3, 4, 5, 6, 7, 8].map((k) => `deck.1.cues.pad${k}`)
	);
	assert.ok(pads.every((n) => n.kind === 'leaf'));
	assert.deepEqual(ids.PAD_SLOT_LETTERS.map(ids.padNumberForSlot), [1, 2, 3, 4, 5, 6, 7, 8]);
	assert.throws(() => ids.deckPadSelId(1, 0), /1-8/);
	assert.throws(() => ids.deckPadSelId(1, 9), /1-8/);
	const tree = createSelectorTree(nodes, 'deck.1.cues');
	assert.equal(tree.drillIn(), 'deck.1.cues.grid-adjust');
	assert.equal(tree.move('right'), 'deck.1.cues.pad1');
	assert.equal(tree.move('right'), 'deck.1.cues.pad5');
});

test('KEYB-13865 beat jump: deck 1 loop holds the beat jump back and forward leaves', () => {
	// [if] deck 1 loop is drilled into [then] the beat jump back and forward buttons are leaves, [else stop].
	const nodes = resolveSelectorNodes(deckOneRecords());
	assert.deepEqual(childIds(nodes, 'deck.1.loop'), [
		'deck.1.loop.cluster',
		'deck.1.loop.jump-back',
		'deck.1.loop.jump-forward'
	]);
	const tree = createSelectorTree(nodes, 'deck.1.loop');
	assert.equal(tree.drillIn(), 'deck.1.loop.cluster');
	assert.equal(tree.move('right'), 'deck.1.loop.jump-back');
	assert.equal(tree.move('right'), 'deck.1.loop.jump-forward');
});

test('KEYB-13865 pad mode menu: a leaf under deck.<n>.cues on every deck', () => {
	// [if] the pad mode menu is shown [then] it is a leaf id under deck.<n>.cues on each of decks 1 to 4, [else stop].
	for (const n of [1, 2, 3, 4]) assert.equal(ids.padModeMenuSelId(n), `deck.${n}.cues.mode`);
	const menu = resolveSelectorNodes(deckOneRecords()).find((n) => n.id === 'deck.1.cues.mode');
	assert.deepEqual([menu?.parentId, menu?.kind], ['deck.1.cues', 'leaf']);
});

test('KEYB-13865 audit list: every deck id is unique and sits under its cues or loop part', () => {
	// [if] a deck's pad-area id list is built [then] ids are unique and prefixed deck.<n>.cues. or deck.<n>.loop., [else stop].
	for (const n of [1, 2, 3, 4]) {
		const list = ids.deckPadSelectorIds(n);
		assert.equal(list.length, 17);
		assert.equal(new Set(list).size, list.length);
		for (const id of list) assert.match(id, new RegExp(`^deck\\.${n}\\.(cues|loop)\\.[a-z0-9-]+$`));
	}
});

test('KEYB-13865 disabled pads: a pad registered with enabled false leaves the tree', () => {
	// [if] pad 3 is busy and registers enabled false [then] the resolved tree drops it and keeps the other 7, [else stop].
	const nodes = resolveSelectorNodes(deckOneRecords((k) => k !== 3));
	const pads = childIds(nodes, 'deck.1.cues').filter((id) => id.includes('.pad'));
	assert.equal(pads.length, 7);
	assert.ok(!pads.includes('deck.1.cues.pad3'));
});
