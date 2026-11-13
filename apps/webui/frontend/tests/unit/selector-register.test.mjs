// requirement: KEYB-13983, KEYB-14023, KEYB-14164, KEYB-14186
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
 * - if describeSelectorSelection lists a command that would not act, or is not plain JSON, then broken
 * - if a batch of node changes does not notify change listeners exactly once then broken
 * - if a zero-area leaf, or a group of only zero-area leaves, reaches the snapshot then broken
 * - if a 0x0 group with one visible leaf (a display: contents wrapper) is dropped then broken
 * - if a two-node or longer parent cycle loops or resolves instead of throwing SelectorParentCycleError then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Worker } from 'node:worker_threads';

import { createSelectorTree } from '../../src/lib/keyboard/selector-tree.ts';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { createSelectorSession } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-session.ts'
);

const {
	DECK_PARTS,
	deckPartNodeIds,
	describeSelectorSelection,
	onSelectorRegistryChange,
	resolveSelectorNodes,
	selectorNode,
	selectorRegistryVersion,
	selId,
	snapshotSelectorNodes,
	unionRect
} = await loadTypeScriptModule('src/lib/keyboard/selector-register.ts');

const rec = (id, parentId, x, y, w, h, kind = 'leaf', enabled = true) => ({
	id,
	parentId,
	kind,
	enabled,
	rect: { x, y, width: w, height: h }
});

/** Laid out like /performance in four-deck mode, records in arbitrary order. */
function performanceFixture({ lessMode = false, allParts = false } = {}) {
	const parts = (n, x, y) =>
		allParts
			? DECK_PARTS.map((part, i) =>
					rec(`deck.${n}.${part}`, `deck.${n}`, x + (i % 3) * 40, y + 20 + i * 25, 80, 20)
				)
			: [
					rec(`deck.${n}.header`, `deck.${n}`, x, y + 20, 500, 75),
					rec(`deck.${n}.cues`, `deck.${n}`, x, y + 120, 200, 100),
					rec(`deck.${n}.jog`, `deck.${n}`, x + 300, y + 120, 100, 100)
				];
	const deck = (n, x, y) => [
		rec(`deck.${n}`, 'decks', x, y, 500, 250, 'group', !(lessMode && n > 2)),
		...parts(n, x, y)
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

/** The read model, checked to survive a JSON round trip unchanged. */
function describe(nodes, selectedId) {
	const snapshot = describeSelectorSelection(nodes, selectedId);
	assert.deepEqual(JSON.parse(JSON.stringify(snapshot)), snapshot, 'snapshot is plain JSON');
	return snapshot;
}

test('#12267 nothing selected: the snapshot has a null id and no operations', () => {
	/** [if] no selector node is selected [then] the snapshot is null id, not present, no kind and no operations, [else stop]. */
	assert.deepEqual(describe(resolveSelectorNodes(performanceFixture()), null), {
		selectedId: null,
		present: false,
		kind: null,
		parentId: null,
		operations: []
	});
});

test('#12267 a leaf lists its moves, drill-out and only the leaf commands that act on it', () => {
	/** [if] a leaf node is selected [then] its moves with a target, drill-out and activate (adjust only on a dial) are listed, [else stop]. */
	const nodes = resolveSelectorNodes([
		...performanceFixture(),
		rec('library.row', 'library', 0, 820, 1400, 30)
	]);
	assert.deepEqual(describe(nodes, 'deck.1.header'), {
		selectedId: 'deck.1.header',
		present: true,
		kind: 'leaf',
		parentId: 'deck.1',
		operations: ['selector.move.down', 'selector.drill-out', 'selector.activate']
	});
	assert.deepEqual(describe(nodes, 'mixer.ch4.low').operations, [
		'selector.drill-out',
		'selector.activate',
		'selector.adjust.up',
		'selector.adjust.down'
	]);
	assert.deepEqual(describe(nodes, 'library.row').operations, ['selector.drill-out']);
});

test('#12267 a section with children lists its moves and drill-in but no drill-out', () => {
	/** [if] a root section with children is selected [then] up, down and drill-in are listed and nothing else, [else stop]. */
	assert.deepEqual(describe(resolveSelectorNodes(performanceFixture()), 'decks'), {
		selectedId: 'decks',
		present: true,
		kind: 'section',
		parentId: null,
		operations: ['selector.move.up', 'selector.move.down', 'selector.drill-in']
	});
});

test('#12267 a selected node that is gone keeps its id flagged not present', () => {
	/** [if] the selected node is no longer registered [then] the snapshot keeps its id with present false and no operations, [else stop]. */
	const nodes = resolveSelectorNodes(performanceFixture({ lessMode: true }));
	assert.deepEqual(describe(nodes, 'deck.3.header'), {
		selectedId: 'deck.3.header',
		present: false,
		kind: null,
		parentId: null,
		operations: []
	});
});

//----- #12543: nested display-contents wrappers measure as the union of visible descendants

/** Fake DOM element: a box when `rect` is given, else a display-contents wrapper. */
function fakeEl(rect, children = []) {
	const el = {
		display: rect ? 'block' : 'contents',
		children,
		parentElement: null,
		attrs: new Map(),
		setAttribute(k, v) {
			this.attrs.set(k, v);
		},
		toggleAttribute(k, on) {
			if (on) this.attrs.set(k, '');
			else this.attrs.delete(k);
		},
		removeAttribute(k) {
			this.attrs.delete(k);
		},
		closest() {
			return null;
		},
		getBoundingClientRect() {
			return rect ? { ...rect } : { x: 0, y: 0, width: 0, height: 0 };
		}
	};
	for (const c of children) c.parentElement = el;
	return el;
}

/** Registers `el` as a root selector node, snapshots it, and returns its measured rect. */
function measure(el) {
	const prev = globalThis.getComputedStyle;
	globalThis.getComputedStyle = (e) => ({ display: e.display });
	const handle = selectorNode(el, { id: 'probe', kind: 'group', enabled: true });
	try {
		return snapshotSelectorNodes().find((n) => n.id === 'probe').rect;
	} finally {
		handle.destroy();
		globalThis.getComputedStyle = prev;
	}
}

test('nested display-contents wrapper measures the union of its visible descendants', () => {
	const box = (x, y, width, height) => fakeEl({ x, y, width, height });
	const wrapper = fakeEl(null, [
		fakeEl(null, [box(10, 20, 30, 40), fakeEl(null, [box(100, 5, 10, 10)])]),
		box(0, 0, 0, 0),
		box(50, 70, 20, 10)
	]);
	assert.deepEqual(measure(wrapper), { x: 10, y: 5, width: 100, height: 75 });
});

test('ordinary element measures its own bounding box, not its children', () => {
	const el = fakeEl({ x: 1, y: 2, width: 3, height: 4 }, [
		fakeEl({ x: 500, y: 500, width: 50, height: 50 })
	]);
	assert.deepEqual(measure(el), { x: 1, y: 2, width: 3, height: 4 });
});

test('#12256 a batch of node changes notifies change listeners once', async () => {
	/** [if] nodes are disabled and removed in one flush [then] listeners run once after it, identical updates stay silent, [else stop]. */
	const fakeEl = () => ({ setAttribute() {}, removeAttribute() {}, toggleAttribute() {} });
	const deck3 = selectorNode(fakeEl(), { id: 'deck.3', kind: 'group', enabled: true });
	const leaf = selectorNode(fakeEl(), { id: 'deck.3.jog', kind: 'leaf', enabled: true });
	await Promise.resolve();
	let calls = 0;
	const stop = onSelectorRegistryChange(() => (calls += 1));
	const version = selectorRegistryVersion();
	deck3.update({ id: 'deck.3', kind: 'group', enabled: true });
	await Promise.resolve();
	assert.equal(calls, 0, 'an identical update is not a layout change');
	assert.equal(selectorRegistryVersion(), version);
	deck3.update({ id: 'deck.3', kind: 'group', enabled: false });
	leaf.destroy();
	assert.equal(calls, 0, 'listeners wait for the batch to finish');
	await Promise.resolve();
	assert.equal(calls, 1);
	stop();
	deck3.destroy();
	await Promise.resolve();
	assert.equal(calls, 1, 'an unsubscribed listener is not called');
});

const idsOf = (records) => resolveSelectorNodes(records).map((n) => n.id);

test('#12539 a leaf with zero width or zero height is dropped from the snapshot', () => {
	/** [if] a leaf has zero width or zero height [then] the snapshot drops it and keeps its parent, [else stop]. */
	const ids = idsOf([
		rec('row', null, 0, 0, 400, 100, 'section'),
		rec('row.thin', 'row', 0, 0, 0, 100),
		rec('row.flat', 'row', 100, 0, 100, 0),
		rec('row.ok', 'row', 200, 0, 100, 100)
	]);
	assert.deepEqual(ids, ['row', 'row.ok']);
});

test('#12539 a group whose children are all zero-area is dropped with them', () => {
	/** [if] a group has only zero-area children [then] the group and its children leave the snapshot, [else stop]. */
	const ids = idsOf([
		rec('decks', null, 0, 0, 800, 400, 'section'),
		rec('deck.1', 'decks', 0, 0, 400, 400, 'group'),
		rec('deck.1.jog', 'deck.1', 0, 0, 100, 100),
		rec('deck.2', 'decks', 400, 0, 400, 400, 'group'),
		rec('deck.2.jog', 'deck.2', 400, 0, 0, 0),
		rec('deck.2.cues', 'deck.2', 500, 0, 0, 80)
	]);
	assert.deepEqual(ids, ['decks', 'deck.1', 'deck.1.jog']);
});

test('#12539 visible sibling leaves and their ancestors stay in the snapshot', () => {
	/** [if] one leaf of a group is zero-area [then] its visible siblings, group and section all stay, [else stop]. */
	const nodes = resolveSelectorNodes(
		performanceFixture().map((r) =>
			r.id === 'deck.1.cues' ? { ...r, rect: { ...r.rect, width: 0 } } : r
		)
	);
	const ids = nodes.map((n) => n.id);
	assert.ok(!ids.includes('deck.1.cues'));
	for (const kept of ['decks', 'deck.1', 'deck.1.header', 'deck.1.jog'])
		assert.ok(ids.includes(kept), kept);
	const tree = createSelectorTree(nodes, 'deck.1.header');
	assert.equal(tree.move('down'), 'deck.1.jog');
});

test('#12539 a 0x0 group with one visible leaf survives and takes that leaf rect', () => {
	/** [if] a display: contents group measures 0x0 but holds one visible leaf [then] it stays with the leaf rect, [else stop]. */
	const nodes = resolveSelectorNodes([
		rec('decks', null, 0, 0, 800, 400, 'section'),
		rec('deck.1.cues', 'decks', 0, 0, 0, 0, 'group'),
		rec('deck.1.cues.a', 'deck.1.cues', 10, 20, 0, 40),
		rec('deck.1.cues.b', 'deck.1.cues', 50, 20, 40, 40)
	]);
	assert.deepEqual(
		nodes.map((n) => n.id),
		['decks', 'deck.1.cues', 'deck.1.cues.b']
	);
	assert.deepEqual(nodes[1].rect, { x: 50, y: 20, width: 40, height: 40 });
});

test('#12539 a selected leaf that turns zero-area is left like a removed one', () => {
	/** [if] the selected leaf turns zero-area and the registry moves [then] the session selects its parent group, [else stop]. */
	const state = { version: 1, records: performanceFixture(), rings: [] };
	const session = createSelectorSession({
		snapshot: () => resolveSelectorNodes(state.records),
		version: () => state.version,
		showRing: (target) => state.rings.push(target.id),
		hideRing: () => state.rings.push(null)
	});
	session.run('selector.drill-in', { timeStamp: 0, startId: 'deck.1.cues' });
	state.records = state.records.map((r) =>
		r.id === 'deck.1.cues' ? { ...r, rect: { ...r.rect, height: 0 } } : r
	);
	state.version += 1;
	assert.equal(session.reconcile(), 'deck.1');
	assert.deepEqual(state.rings, ['deck.1.cues', 'deck.1']);
});

//----- #12538 parent cycles (resolved in a worker so a loop fails on a timeout, not a hung runner)

const LOOP_TIMEOUT_MS = 3000;
const LOADER_URL = new URL('./load-typescript.mjs', import.meta.url).href;
const CYCLE_WORKER_SOURCE = `
const { parentPort, workerData } = require('node:worker_threads');
import(workerData.loaderUrl)
	.then(({ loadTypeScriptModule }) => loadTypeScriptModule('src/lib/keyboard/selector-register.ts'))
	.then(({ resolveSelectorNodes }) => {
		parentPort.postMessage({ ready: true });
		try {
			const ids = resolveSelectorNodes(workerData.records).map((n) => n.id);
			parentPort.postMessage({ ids });
		} catch (error) {
			parentPort.postMessage({ error: { name: error.name, message: error.message } });
		}
	});
`;

/** Resolves `records` off-thread: `{ ids }`, `{ error }`, or `{ looped: true }` after the timeout. */
function resolveOffThread(records) {
	return new Promise((done, fail) => {
		const worker = new Worker(CYCLE_WORKER_SOURCE, {
			eval: true,
			workerData: { loaderUrl: LOADER_URL, records }
		});
		let timer;
		worker.on('message', (msg) => {
			if (msg.ready) {
				timer = setTimeout(
					() => worker.terminate().then(() => done({ looped: true })),
					LOOP_TIMEOUT_MS
				);
				return;
			}
			clearTimeout(timer);
			worker.terminate().then(() => done(msg));
		});
		worker.on('error', fail);
	});
}

test('#12538 two records that name each other as parent throw a named cycle error', async () => {
	/** [if] two selector records name each other as parent and child [then] resolve throws SelectorParentCycleError naming both, [else stop]. */
	const outcome = await resolveOffThread([
		rec('decks', null, 0, 0, 800, 400, 'section'),
		rec('deck.1', 'deck.1.jog', 0, 0, 400, 400, 'group'),
		rec('deck.1.jog', 'deck.1', 0, 0, 100, 100, 'group')
	]);
	assert.deepEqual(outcome, {
		error: {
			name: 'SelectorParentCycleError',
			message: 'Selector parent cycle: deck.1 -> deck.1.jog -> deck.1'
		}
	});
});

test('#12538 a longer parent cycle with a tail into it throws a named cycle error', async () => {
	/** [if] a three-node parent cycle is supplied, reached through a tail node [then] resolve throws naming only the cycle, [else stop]. */
	const outcome = await resolveOffThread([
		rec('decks', null, 0, 0, 800, 400, 'section'),
		rec('tail', 'a', 0, 0, 10, 10),
		rec('a', 'c', 0, 0, 100, 100, 'group'),
		rec('b', 'a', 0, 0, 100, 100, 'group'),
		rec('c', 'b', 0, 0, 100, 100, 'group')
	]);
	assert.deepEqual(outcome, {
		error: { name: 'SelectorParentCycleError', message: 'Selector parent cycle: a -> c -> b -> a' }
	});
});

test('#12538 an acyclic registered tree resolves to the same snapshot as before', async () => {
	/** [if] an acyclic registered selector tree is supplied [then] resolve returns the unchanged node list, [else stop]. */
	const expected = [
		...['topbar', 'waveforms', 'decks', 'library'],
		...['deck.3', 'deck.3.header', 'deck.3.cues', 'deck.3.jog'],
		...[
			'deck.1',
			'deck.1.header',
			'deck.1.cues',
			'deck.1.jog',
			'mixer',
			'mixer.ch4',
			'mixer.ch4.low'
		],
		...['deck.2', 'deck.2.header', 'deck.2.cues', 'deck.2.jog'],
		...['deck.4', 'deck.4.header', 'deck.4.cues', 'deck.4.jog']
	];
	assert.deepEqual(await resolveOffThread(performanceFixture()), { ids: expected });
	assert.deepEqual(idsOf(performanceFixture()), expected);
});

test('[if] decks drills into a deck group [then] the next drill hits an inventory deck part, [else stop]', () => {
	const nodes = resolveSelectorNodes(performanceFixture({ allParts: true }));
	const tree = createSelectorTree(nodes, 'decks');
	assert.equal(tree.drillIn(), 'deck.1');
	const part = tree.drillIn();
	assert.ok(deckPartNodeIds(1).includes(part), `expected inventory part, got ${part}`);
});

test('[if] each declared deck part is registered [then] deckPartNodeIds matches DECK_PARTS, [else stop]', () => {
	assert.deepEqual(
		deckPartNodeIds(2),
		DECK_PARTS.map((part) => selId.deckPart(2, part))
	);
	const ids = resolveSelectorNodes(performanceFixture({ allParts: true })).map((n) => n.id);
	for (const partId of deckPartNodeIds(1)) {
		assert.ok(ids.includes(partId), `${partId} missing from resolved fixture`);
	}
});

test('[if] a deck is disabled by layout [then] every inventory part id is dropped, [else stop]', () => {
	const ids = resolveSelectorNodes(performanceFixture({ lessMode: true, allParts: true })).map(
		(n) => n.id
	);
	for (const partId of [...deckPartNodeIds(3), ...deckPartNodeIds(4)]) {
		assert.ok(!ids.includes(partId), `${partId} should be dropped`);
	}
});

//----- #12541: the debug selector tree rebuilds when the registry version moves

const { createLiveSelectorTree } = await loadTypeScriptModule(
	'src/lib/keyboard/selector-register.ts'
);

test('createLiveSelectorTree (#12541)', async (t) => {
	/** Builds a snapshot/version pair the test mutates like mount, destroy and update would. */
	function liveFixture(initialId) {
		const state = { lessMode: false, version: 0, snapshots: 0 };
		const snapshot = () => {
			state.snapshots++;
			return resolveSelectorNodes(performanceFixture({ lessMode: state.lessMode }));
		};
		return { state, get: createLiveSelectorTree(snapshot, () => state.version, initialId) };
	}

	await t.test('a destroyed selected node falls back to its nearest surviving ancestor', () => {
		const { state, get } = liveFixture('deck.4.header');
		assert.equal(get().selected(), 'deck.4.header');
		state.lessMode = true;
		state.version++;
		assert.equal(get().selected(), 'decks', 'deck.4 subtree is gone, so decks is selected');
	});

	await t.test('an enabled to disabled change drops the subtree from later moves', () => {
		const { state, get } = liveFixture('deck.1');
		state.lessMode = true;
		state.version++;
		for (let i = 0; i < 4; i++) {
			const id = get().move('down');
			assert.ok(!id.startsWith('deck.3') && !id.startsWith('deck.4'), `moved onto ${id}`);
		}
		assert.equal(get().selected(), 'deck.1', 'deck.3 below deck.1 is disabled');
	});

	await t.test('unchanged nodes between moves reuse the same tree and snapshot', () => {
		const { state, get } = liveFixture();
		const first = get();
		get().move('down');
		assert.equal(get(), first);
		assert.equal(state.snapshots, 1);
		state.version++;
		assert.notEqual(get(), first);
		assert.equal(get().selected(), 'waveforms', 'a surviving selection is kept');
		assert.equal(state.snapshots, 2);
	});
});
