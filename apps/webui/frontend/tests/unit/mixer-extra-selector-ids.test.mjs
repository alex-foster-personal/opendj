// requirement: KEYB-13901
/**
 * #13706: assign matrix, crossfade curve, group pad and edge sampler selector nodes.
 *
 * Regression lines:
 * - if drilling into the mixer does not list both assign groups, the curve leaf and the group pad leaf then broken
 * - if a visible edge sampler is not reachable by a move, or its pads are not leaves, then broken
 * - if a hidden group pad, settings copy or switched-off sampler keeps a live node then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const {
	assignMatrixSelectorId,
	assignSwitchSelectorId,
	crossfadeCurveSelectorId,
	groupPadSelectorId
} = await loadTypeScriptModule('src/lib/components/rb/mixer/mixer-extra-selector-ids.ts');
const { edgeSamplerPadSelectorId, edgeSamplerSelectorId } = await loadTypeScriptModule(
	'src/lib/components/rb/sampler/edge-sampler-selector-ids.ts'
);
const { resolveSelectorNodes, selId } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-register.ts'
);

const W = 1440;
const H = 1000;
const PADS = 10;

const rec = (id, parentId, x, y, w, h, kind = 'leaf', enabled = true) => ({
	id,
	parentId,
	kind,
	enabled,
	rect: { x, y, width: w, height: h }
});

/** The mixer lower row (assign A, curve, crossfader, assign B) plus the open group pad. */
function mixerFixture() {
	const out = [
		rec(selId.section('decks'), null, 0, 200, W, 500, 'section'),
		rec(selId.mixer(), selId.section('decks'), 580, 200, 280, 500, 'group'),
		rec(selId.channel(1), selId.mixer(), 590, 220, 60, 380, 'group'),
		rec(selId.headphones(), selId.mixer(), 590, 610, 260, 30),
		rec(crossfadeCurveSelectorId(), selId.mixer(), 640, 660, 60, 20),
		rec(selId.crossfader(), selId.mixer(), 705, 655, 100, 30)
	];
	for (const [bus, x] of [
		['A', 600],
		['B', 815]
	]) {
		out.push(rec(assignMatrixSelectorId(bus), selId.mixer(), x, 655, 32, 28, 'group'));
		for (const deck of [1, 2, 3, 4]) {
			const col = (deck - 1) % 2;
			const row = deck > 2 ? 1 : 0;
			out.push(
				rec(
					assignSwitchSelectorId(bus, deck),
					assignMatrixSelectorId(bus),
					x + col * 17,
					655 + row * 15,
					15,
					13
				)
			);
		}
	}
	out.push(rec(groupPadSelectorId(), selId.mixer(), W - 180, 48, 168, 200));
	return out;
}

/** Page roots plus both sampler edges, each a zero-width pad column from y 76 down. */
function pageFixture({ samplerEnabled = true } = {}) {
	const out = [
		rec(selId.section('topbar'), null, 0, 0, W, 28, 'section'),
		rec(selId.section('waveforms'), null, 0, 28, W, 160, 'section'),
		rec(selId.section('decks'), null, 0, 190, W, 500, 'section'),
		rec(selId.section('library'), null, 0, 700, W, 300, 'section')
	];
	for (const [edge, x] of [
		['L', 0],
		['R', W]
	]) {
		out.push(rec(edgeSamplerSelectorId(edge), null, x, 76, 0, H - 96, 'group', samplerEnabled));
		for (let i = 0; i < PADS; i++) {
			out.push(
				rec(
					edgeSamplerPadSelectorId(edge, i),
					edgeSamplerSelectorId(edge),
					edge === 'L' ? 0 : W - 3,
					76 + i * 90,
					3,
					90
				)
			);
		}
	}
	return out;
}

const childIds = (nodes, parentId) =>
	nodes.filter((node) => node.parentId === parentId).map((node) => node.id);
const kindOf = (nodes, id) => nodes.find((node) => node.id === id)?.kind;

/** [if] the mixer group is drilled into [then] assign groups, the curve select and the group pad are its children, switches leaves, [else stop]. */
test('KEYB #13706: the mixer lists assign switches, curve select and group pad', () => {
	assert.equal(assignMatrixSelectorId('A'), 'mixer.assign.a');
	assert.equal(assignSwitchSelectorId('B', 3), 'mixer.assign.b.ch3');
	assert.equal(crossfadeCurveSelectorId(), 'mixer.crossfade-curve');
	assert.equal(groupPadSelectorId(), 'mixer.group-pad');
	const nodes = resolveSelectorNodes(mixerFixture());
	const kids = childIds(nodes, 'mixer');
	for (const id of ['mixer.assign.a', 'mixer.assign.b', 'mixer.crossfade-curve', 'mixer.group-pad'])
		assert.ok(kids.includes(id), id);
	assert.equal(kindOf(nodes, 'mixer.crossfade-curve'), 'leaf');
	assert.equal(kindOf(nodes, 'mixer.group-pad'), 'leaf');
	for (const bus of ['a', 'b']) {
		const switches = childIds(nodes, `mixer.assign.${bus}`).sort();
		assert.deepEqual(
			switches,
			[1, 2, 3, 4].map((d) => `mixer.assign.${bus}.ch${d}`)
		);
		for (const id of switches) assert.equal(kindOf(nodes, id), 'leaf', id);
	}
	const tree = createSelectorTree(nodes, 'mixer.crossfade-curve');
	assert.equal(tree.move('left'), 'mixer.assign.a');
	assert.equal(tree.drillIn(), 'mixer.assign.a.ch1');
	assert.equal(tree.move('right'), 'mixer.assign.a.ch2');
});

/** [if] the sampler is visible [then] each edge is a group reached by a move from the decks and its pads are leaves, [else stop]. */
test('KEYB #13706: a visible edge sampler is a reachable group of pad leaves', () => {
	assert.equal(edgeSamplerSelectorId('L'), 'sampler.l');
	assert.equal(edgeSamplerPadSelectorId('R', 0), 'sampler.r.pad1');
	assert.throws(() => edgeSamplerPadSelectorId('L', -1), /Invalid edge sampler pad index/);
	const nodes = resolveSelectorNodes(pageFixture());
	assert.equal(nodes[0].id, 'topbar', 'the sampler never takes the first selection');
	const tree = createSelectorTree(nodes, 'topbar');
	assert.equal(tree.move('down'), 'waveforms', 'a move down from the top bar is not hijacked');
	const fromDecks = createSelectorTree(nodes, 'decks');
	assert.equal(fromDecks.move('left'), 'sampler.l');
	assert.equal(fromDecks.drillIn(), 'sampler.l.pad1');
	assert.equal(fromDecks.move('down'), 'sampler.l.pad2');
	assert.equal(createSelectorTree(nodes, 'decks').move('right'), 'sampler.r');
	for (const edge of ['l', 'r']) {
		const pads = childIds(nodes, `sampler.${edge}`);
		assert.equal(pads.length, PADS);
		for (const id of pads) assert.equal(kindOf(nodes, id), 'leaf', id);
	}
});

/** [if] the group pad is closed, is the settings copy, or the sampler is off [then] its nodes are not live, [else stop]. */
test('KEYB #13706: hidden controls leave no live node', () => {
	const settingsCopy = resolveSelectorNodes([
		rec(groupPadSelectorId(), null, 1200, 48, 168, 200, 'leaf', false)
	]);
	assert.deepEqual(settingsCopy, []);
	const off = resolveSelectorNodes(pageFixture({ samplerEnabled: false })).map((n) => n.id);
	assert.deepEqual(
		off.filter((id) => id.startsWith('sampler.')),
		[]
	);
	assert.deepEqual(off, ['topbar', 'waveforms', 'decks', 'library']);
});
