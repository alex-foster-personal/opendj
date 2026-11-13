// requirement: KEYB-13897
/**
 * #13700: the /performance topbar controls are keyboard selector leaves under `topbar`.
 *
 * Regression lines:
 * - if drilling into the topbar does not land on its leftmost control then broken
 * - if moving right from a topbar leaf does not reach the next control to its right then broken
 * - if a control hidden by a layout tier or disabled stays a live node then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { TOPBAR_SELECTOR_NAMES, topbarNodeEnabled, topbarSelId } = await loadTypeScriptModule(
	'src/lib/components/rb/topbar-selector-node.ts'
);
const { resolveSelectorNodes, selId } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-register.ts'
);

const BAR_H = 28;
/** Control heights as the 28px bar centres them: the tallest (the account bauble) sits right. */
const HEIGHTS = { 'analysis-source': 16, mode: 18, posture: 14, recorder: 14, account: 22 };

/** The topbar row left to right; `hidden` names render 0x0, `disabled` names pass disabled. */
function topbarFixture({ hidden = [], disabled = [] } = {}) {
	const records = [
		{
			id: selId.section('topbar'),
			parentId: null,
			kind: 'section',
			enabled: true,
			rect: { x: 0, y: 0, width: 1600, height: BAR_H }
		}
	];
	let x = 4;
	for (const name of TOPBAR_SELECTOR_NAMES) {
		const h = HEIGHTS[name] ?? 16;
		const shown = !hidden.includes(name);
		const rect = shown
			? { x, y: (BAR_H - h) / 2, width: 40, height: h }
			: { x: 0, y: 0, width: 0, height: 0 };
		if (shown) x += 48;
		records.push({
			id: topbarSelId(name),
			parentId: selId.section('topbar'),
			kind: 'leaf',
			enabled: topbarNodeEnabled(rect, disabled.includes(name)),
			rect
		});
	}
	return records;
}

/** [if] the topbar section is selected and E (drill in) is pressed [then] the selection is its leftmost control, [else stop]. */
test('KEYB #13700: drilling into the topbar lands on its first control in reading order', () => {
	assert.equal(topbarSelId('analysis-source'), 'topbar.analysis-source');
	assert.equal(new Set(TOPBAR_SELECTOR_NAMES).size, TOPBAR_SELECTOR_NAMES.length);
	assert.ok(!TOPBAR_SELECTOR_NAMES.includes('sampler'), 'the sampler chip is waveforms.sampler');
	const nodes = resolveSelectorNodes(topbarFixture());
	const kids = nodes.filter((n) => n.parentId === 'topbar').map((n) => n.id);
	assert.deepEqual(
		kids,
		TOPBAR_SELECTOR_NAMES.map((name) => `topbar.${name}`)
	);
	const tree = createSelectorTree(nodes, 'topbar');
	assert.equal(tree.drillIn(), 'topbar.analysis-source', 'not the tallest control (account)');
});

/** [if] a topbar leaf is selected and D (move right) is pressed [then] the next control to its right is selected, [else stop]. */
test('KEYB #13700: moving right walks the topbar controls left to right', () => {
	const tree = createSelectorTree(resolveSelectorNodes(topbarFixture()), 'topbar');
	tree.drillIn();
	for (const name of TOPBAR_SELECTOR_NAMES.slice(1)) {
		assert.equal(tree.move('right'), topbarSelId(name));
	}
	assert.equal(tree.move('right'), 'topbar.account', 'the rightmost control stays put');
});

/** [if] a topbar control is hidden by a layout tier or disabled [then] it is not a live node and D skips it, [else stop]. */
test('KEYB #13700: a hidden or disabled topbar control is not a live node', () => {
	assert.equal(topbarNodeEnabled({ x: 0, y: 0, width: 0, height: 0 }, false), false);
	assert.equal(topbarNodeEnabled({ x: 9, y: 6, width: 40, height: 16 }, true), false);
	assert.equal(topbarNodeEnabled({ x: 9, y: 6, width: 40, height: 16 }, false), true);
	const nodes = resolveSelectorNodes(topbarFixture({ hidden: ['lyrics'], disabled: ['jobs'] }));
	const ids = nodes.map((n) => n.id);
	assert.ok(!ids.includes('topbar.lyrics'), 'hidden lyrics survived');
	assert.ok(!ids.includes('topbar.jobs'), 'disabled jobs survived');
	const tree = createSelectorTree(nodes, 'topbar.beat-sync-max');
	assert.equal(tree.move('right'), 'topbar.autoplay');
	assert.equal(tree.move('right'), 'topbar.pads');
	assert.equal(tree.move('right'), 'topbar.command');
});
