// requirement: KEYB-13846
/**
 * #13696: channel strip cue, colour FX and R/M calibration selector nodes.
 *
 * Regression lines:
 * - if channelButtonSelectorIds(1) does not give mixer.ch1.cue, .fx and .cal then broken
 * - if the cue leaf is not reachable from the fader region by moves alone then broken
 * - if a disabled channel or the LESS-hidden cal group keeps its new leaves then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { channelButtonSelectorIds } = await loadTypeScriptModule(
	'src/lib/components/rb/mixer/channel-button-selector-ids.ts'
);
const { resolveSelectorNodes, selId } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-register.ts'
);

const rec = (id, parentId, x, y, w, h, kind = 'leaf', enabled = true) => ({
	id,
	parentId,
	kind,
	enabled,
	rect: { x, y, width: w, height: h }
});

/** One MORE-layout channel strip, top to bottom as ChannelStrip.svelte renders it. */
function channelFixture(n, { channelEnabled = true, calEnabled = true } = {}) {
	const ch = selId.channel(n);
	const ids = channelButtonSelectorIds(n);
	const x = 600 + (n - 1) * 120;
	return [
		rec('mixer', null, 580, 280, 500, 520, 'section'),
		rec(ch, 'mixer', x, 300, 110, 480, 'group', channelEnabled),
		rec(ids.cal, ch, x + 60, 302, 20, 10, 'group', calEnabled),
		rec(ids.calRed, ids.cal, x + 60, 302, 9, 10),
		rec(ids.calCeiling, ids.cal, x + 71, 302, 9, 10),
		rec(selId.channelPart(n, 'trim'), ch, x + 25, 320, 60, 60),
		rec(selId.channelPart(n, 'high'), ch, x + 40, 390, 30, 30),
		rec(selId.channelPart(n, 'mid'), ch, x + 40, 425, 30, 30),
		rec(selId.channelPart(n, 'low'), ch, x + 40, 460, 30, 30),
		rec(ids.fx, ch, x + 15, 500, 80, 40, 'group'),
		rec(selId.channelPart(n, 'filter'), ids.fx, x + 15, 500, 40, 40),
		rec(ids.cue, ch, x + 35, 550, 40, 12),
		rec(selId.channelPart(n, 'fader'), ch, x + 35, 570, 40, 200)
	];
}

const childIds = (nodes, parentId) =>
	nodes.filter((node) => node.parentId === parentId).map((node) => node.id);

/** [if] channel 1 is drilled into [then] its children include mixer.ch1.cue, mixer.ch1.fx and mixer.ch1.cal, [else stop]. */
test('KEYB #13696: channel 1 children include cue, fx and cal', () => {
	const ids = channelButtonSelectorIds(1);
	assert.deepEqual(ids, {
		cue: 'mixer.ch1.cue',
		fx: 'mixer.ch1.fx',
		cal: 'mixer.ch1.cal',
		calRed: 'mixer.ch1.cal.red',
		calCeiling: 'mixer.ch1.cal.ceiling'
	});
	const nodes = resolveSelectorNodes(channelFixture(1));
	const kids = childIds(nodes, 'mixer.ch1');
	for (const id of ['mixer.ch1.cue', 'mixer.ch1.fx', 'mixer.ch1.cal'])
		assert.ok(kids.includes(id), id);
	assert.deepEqual(childIds(nodes, 'mixer.ch1.cal').sort(), [
		'mixer.ch1.cal.ceiling',
		'mixer.ch1.cal.red'
	]);
	const tree = createSelectorTree(nodes, 'mixer.ch1');
	assert.equal(tree.drillIn(), 'mixer.ch1.cal', 'R|M sits top right, so it is the top-left child');
});

/** [if] the selector moves from the fader region of channel 1 [then] it lands on mixer.ch1.cue by moves alone, [else stop]. */
test('KEYB #13696: the cue button is reachable from the fader region by moves', () => {
	const nodes = resolveSelectorNodes(channelFixture(1));
	const fromLow = createSelectorTree(nodes, 'mixer.ch1.low');
	assert.equal(fromLow.move('down'), 'mixer.ch1.fx');
	assert.equal(fromLow.move('down'), 'mixer.ch1.cue');
	assert.equal(fromLow.move('down'), 'mixer.ch1.fader');
	const fromFader = createSelectorTree(nodes, 'mixer.ch1.fader');
	assert.equal(fromFader.move('up'), 'mixer.ch1.cue');
});

/** [if] the channel is disabled or LESS hides R|M [then] the new leaves leave the snapshot with their parent, [else stop]. */
test('KEYB #13696: a disabled channel or hidden cal group drops its new leaves', () => {
	const ids = channelButtonSelectorIds(3);
	const off = resolveSelectorNodes(channelFixture(3, { channelEnabled: false })).map((n) => n.id);
	for (const id of Object.values(ids))
		assert.ok(!off.includes(id), `${id} survived a disabled channel`);
	assert.deepEqual(off, ['mixer']);
	const less = resolveSelectorNodes(channelFixture(3, { calEnabled: false })).map((n) => n.id);
	for (const id of [ids.cal, ids.calRed, ids.calCeiling]) assert.ok(!less.includes(id), id);
	for (const id of [ids.cue, ids.fx]) assert.ok(less.includes(id), id);
});
