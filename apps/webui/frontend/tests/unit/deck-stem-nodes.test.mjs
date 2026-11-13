/**
 * requirement: KEYB-14494 (#14471, epic #9372): a deck's stem row is a selector group with one leaf per button.
 *
 * The real StemRow.svelte is client-compiled and mounted on the node DOM shim, so its real
 * `selectorNode` actions run and the test reads the nodes the page would register. Deck.svelte
 * (too heavy to mount here) takes its stems part kind from `deckStemsPartKind`, checked directly.
 *
 * Regression lines:
 * - if deck 1 stems does not hold one leaf per visible stem mute chip then broken
 * - if a StemRow with no selector prefix (the channel strip) registers any selector node then broken
 * - if Enter on the INST quick mode leaf presses anything but the INST quick button then broken
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { bundleStemRowClient } from './mount-svelte-client.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';

const STEM_IDS = ['vocal', 'instrumental', 'drums', 'bass', 'other'];

let client;
let teardownDom;

before(async () => {
	teardownDom = installQualityBadgeDom();
	// The shim lacks toggleAttribute, which the selectorNode action uses for data-sel-disabled.
	globalThis.Element.prototype.toggleAttribute = function (name, force) {
		if (force) this.setAttribute(name, '');
		else this.removeAttribute(name);
		return force;
	};
	client = await bundleStemRowClient();
});

after(() => {
	delete globalThis.Element.prototype.toggleAttribute;
	teardownDom?.();
});

/** Every element under `root` (itself included) in DOM order. */
function walk(root, out = []) {
	if (root.nodeType === 1) out.push(root);
	for (const child of root.childNodes ?? []) walk(child, out);
	return out;
}

/** The selector nodes the mounted markup registered: id, kind, nearest node ancestor, element. */
function selectorNodes(root) {
	return walk(root)
		.filter((el) => el.hasAttribute('data-sel-node'))
		.map((el) => {
			let up = el.parentElement;
			while (up !== null && !up.hasAttribute('data-sel-node')) up = up.parentElement;
			return {
				id: el.getAttribute('data-sel-node'),
				kind: el.getAttribute('data-sel-kind'),
				disabled: el.hasAttribute('data-sel-disabled'),
				parentId: up === null ? null : up.getAttribute('data-sel-node'),
				el
			};
		});
}

const buttonsIn = (el) => walk(el).filter((n) => n.nodeName.toLowerCase() === 'button');

/** A deck whose precomputed Demucs stems are loaded (all five controls separate). */
function separatedDeck() {
	const ctl = () => ({ muted: false, solo: false, gain: 0.5, fx_send: 0 });
	return {
		deck_id: 1,
		stems: {
			status: 'ready',
			source: 'demucs',
			model: 'htdemucs',
			layout: 'demucs4',
			available_controls: [...STEM_IDS],
			alignment: null,
			controls: Object.fromEntries(STEM_IDS.map((id) => [id, ctl()])),
			error: null,
			load: null
		}
	};
}

const noop = async () => {};

/** Mount StemRow inside `host` (a stand-in for the parent's wrapper) and unmount after the test. */
function mountStemRow(t, host, props) {
	document.body.appendChild(host);
	const instance = client.mount(client.StemRow, {
		target: host,
		props: {
			deck: separatedDeck(),
			pending: false,
			onMute: noop,
			onSolo: noop,
			onGain: () => {},
			...props
		}
	});
	client.flushSync();
	t.after(() => {
		client.unmount(instance);
		host.remove();
	});
	return host;
}

/** A stand-in for Deck.svelte's stems part wrapper, registered as a group. */
function stemsGroup() {
	const group = document.createElement('div');
	group.setAttribute('data-sel-node', 'deck.1.stems');
	group.setAttribute('data-sel-kind', 'group');
	return group;
}

test('KEYB-14494 stems group: deck 1 stems holds one leaf per visible stem mute chip', (t) => {
	// [if] a deck with separated stems renders [then] deck.<n>.stems is a group with one leaf per visible stem mute button, [else stop].
	const root = mountStemRow(t, stemsGroup(), {
		selectorPrefix: 'deck.1.stems',
		onQuickMode: noop,
		onLoad: noop
	});
	const leaves = selectorNodes(root).filter((n) => n.id !== 'deck.1.stems');
	assert.ok(leaves.every((n) => n.kind === 'leaf' && n.parentId === 'deck.1.stems' && !n.disabled));
	const chipLeaves = STEM_IDS.map((stem) => {
		const leaf = leaves.find((n) => n.id === client.ids.stemRowSelId('deck.1.stems', stem));
		assert.ok(leaf, `no leaf for the ${stem} mute chip`);
		return buttonsIn(leaf.el).map((b) => b.getAttribute('data-testid'));
	});
	assert.deepEqual(
		chipLeaves,
		STEM_IDS.map((stem) => [`stem-${stem}-deck-1`])
	);
	assert.deepEqual(
		leaves.map((n) => n.id.slice('deck.1.stems.'.length)),
		[...STEM_IDS, 'quick.aca', 'quick.inst', 'quick.all', 'reset']
	);
	assert.equal(
		client.ids.deckStemsPartKind(false),
		'group',
		'the Svelte deck stems part is not a group'
	);
	assert.equal(
		client.ids.deckStemsPartKind(true),
		'leaf',
		'the React twin registers no leaves, so its part stays a leaf'
	);
});

test('KEYB-14494 channel strip: a StemRow with no selector prefix registers no selector nodes', (t) => {
	// [if] StemRow renders with no selector prefix (the channel strip case) [then] it registers no selector nodes, [else stop].
	const root = mountStemRow(t, document.createElement('div'), { testIdScope: 'channel' });
	assert.ok(buttonsIn(root).length >= STEM_IDS.length + 1, 'the row did not render its chips');
	assert.deepEqual(selectorNodes(root), []);
});

test('KEYB-14494 quick leaf: Enter on the INST quick mode leaf presses the INST quick button', async (t) => {
	// [if] the leaf for a quick stem mode is activated [then] that quick mode button is the control clicked, [else stop].
	const calls = [];
	const root = mountStemRow(t, stemsGroup(), {
		selectorPrefix: 'deck.1.stems',
		onMute: async (stem) => void calls.push(`mute-${stem}`),
		onQuickMode: async (mode) => void calls.push(`quick-${mode}`)
	});
	const leafId = client.ids.stemRowSelId('deck.1.stems', 'quick.inst');
	const leaf = selectorNodes(root).find((n) => n.id === leafId);
	assert.ok(leaf, `no ${leafId} leaf`);
	// Enter activates the leaf's first enabled control in DOM order (selector-activate, #13708).
	const buttons = buttonsIn(leaf.el);
	assert.equal(buttons.length, 1, 'the quick leaf does not wrap exactly one button');
	const primary = buttons.find((b) => !b.hasAttribute('disabled'));
	assert.equal(primary.getAttribute('data-testid'), 'stem-quick-inst-deck-1');
	const path = [];
	for (let n = primary; n !== null; n = n.parentNode) path.push(n);
	primary.dispatchEvent({
		type: 'click',
		bubbles: true,
		timeStamp: 1,
		shiftKey: false,
		composedPath: () => path
	});
	await Promise.resolve();
	assert.deepEqual(calls, ['quick-inst']);
});
