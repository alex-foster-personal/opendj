/**
 * requirement: KEYB-14374 (#14331, epic #9372): a deck's transport and jog parts are selector groups with one leaf per button.
 *
 * The real TransportCluster.svelte and JogDial.svelte are client-compiled and mounted on the
 * node DOM shim, so their real `selectorNode` actions run and the test reads the nodes the
 * page would register. Deck.svelte (too heavy to mount here) takes its transport and jog part
 * kind from `deckTransportJogPartKind`, checked directly.
 *
 * Regression lines:
 * - if deck 1 transport does not hold the cue, call-prev, call-next and play leaves then broken
 * - if Enter on the play leaf presses anything but the play button then broken
 * - if deck 1 jog does not hold dial, quantize, slip and master-tempo leaves of one control each then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { transform } from 'esbuild';
import { compile, compileModule } from 'svelte/compiler';

import { build } from './esbuild-tsconfig.mjs';
import { importBundledSource } from './import-bundled-source.mjs';
import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

/** Client-compile `.svelte` components and `.svelte.ts` rune modules into one browser bundle. */
const svelteClientPlugin = {
	name: 'deck-transport-jog-client',
	setup(b) {
		b.onLoad({ filter: /\.svelte\.ts$/ }, async (args) => {
			const ts = await transform(readFileSync(args.path, 'utf8'), {
				loader: 'ts',
				sourcefile: args.path
			});
			const out = compileModule(ts.code, {
				generate: 'client',
				filename: args.path.replace(/\.ts$/, '.js')
			});
			return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
		b.onLoad({ filter: /\.svelte$/ }, (args) => {
			const out = compile(readFileSync(args.path, 'utf8'), {
				filename: args.path,
				generate: 'client',
				css: 'injected'
			});
			return { contents: out.js.code, loader: 'js', resolveDir: dirname(args.path) };
		});
	}
};

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
	globalThis.requestAnimationFrame ??= () => 0;
	globalThis.cancelAnimationFrame ??= () => {};
	// prefs.svelte reads window.localStorage at import; the shim's window is globalThis.
	const store = new Map();
	const localStorage = {
		getItem: (k) => store.get(k) ?? null,
		setItem: (k, v) => void store.set(k, String(v)),
		removeItem: (k) => void store.delete(k)
	};
	Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: localStorage });
	globalThis.devicePixelRatio = 1;
	globalThis.matchMedia ??= () => ({
		matches: false,
		addEventListener() {},
		removeEventListener() {}
	});
	const result = await build({
		stdin: {
			contents: `export { mount, unmount, flushSync } from 'svelte';
export { default as TransportCluster } from '$lib/components/rb/deck/TransportCluster.svelte';
export { default as JogDial } from '$lib/components/rb/deck/JogDial.svelte';
export { _emptyDeckState } from '$lib/player/state.svelte';
export * as ids from '$lib/components/rb/deck/deck-transport-jog-selector-ids';`,
			resolveDir: FRONTEND_ROOT,
			loader: 'js',
			sourcefile: 'deck-transport-jog-entry.mjs'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			'import.meta.env.DEV': 'false',
			'import.meta.env.VITE_API_BASE': JSON.stringify('https://deck-transport-jog.example.test')
		},
		format: 'esm',
		logLevel: 'silent',
		platform: 'browser',
		conditions: ['browser'],
		plugins: [viteUrlSuffixPlugin, svelteClientPlugin],
		target: 'es2022',
		write: false
	});
	client = await importBundledSource(result.outputFiles[0].text, 'deck-transport-jog-client');
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

/** Mount `Component` inside a stand-in for Deck.svelte's part wrapper, registered as a group. */
function mountInPart(t, Component, partId, props) {
	const group = document.createElement('div');
	group.setAttribute('data-sel-node', partId);
	group.setAttribute('data-sel-kind', 'group');
	document.body.appendChild(group);
	const instance = client.mount(Component, { target: group, props });
	client.flushSync();
	t.after(() => {
		client.unmount(instance);
		group.remove();
	});
	return group;
}

function loadedDeck() {
	return { ...client._emptyDeckState(1), stable_id: 'track-1', duration_ms: 240000 };
}

const noop = async () => {};

test('KEYB-14374 transport: deck 1 transport is a group of cue, call-prev, call-next and play leaves', (t) => {
	// [if] a deck renders [then] deck.<n>.transport is a group whose children are the cue, call-prev, call-next and play leaves, [else stop].
	const deck = loadedDeck();
	const props = {
		deck,
		pending: false,
		quantizedLaunchArmed: null,
		onCue: noop,
		onMemoryCueCall: noop,
		onPlayPause: noop
	};
	const root = mountInPart(t, client.TransportCluster, 'deck.1.transport', props);
	const leaves = selectorNodes(root).filter((n) => n.id !== 'deck.1.transport');
	assert.deepEqual(
		leaves.map((n) => [n.id, n.kind, n.parentId, n.disabled]),
		client.ids.TRANSPORT_SEL_CONTROLS.map((c) => [
			`deck.1.transport.${c}`,
			'leaf',
			'deck.1.transport',
			false
		])
	);
	const testIds = leaves.map((n) => buttonsIn(n.el).map((b) => b.getAttribute('data-testid')));
	assert.deepEqual(testIds, [
		['cue-deck-1'],
		['memory-cue-call-prev-deck-1'],
		['memory-cue-call-next-deck-1'],
		['play-deck-1']
	]);
	assert.equal(
		client.ids.deckTransportJogPartKind(),
		'group',
		'Deck transport and jog parts are not groups'
	);
});

test('KEYB-14374 play leaf: Enter on the play leaf presses play, not cue', async (t) => {
	// [if] the play leaf is selected and Enter is pressed [then] the play button is the control activated, not cue, [else stop].
	const calls = [];
	const props = {
		deck: loadedDeck(),
		pending: false,
		quantizedLaunchArmed: null,
		onCue: async () => void calls.push('cue'),
		onMemoryCueCall: async (direction) => void calls.push(`call-${direction}`),
		onPlayPause: async () => void calls.push('play')
	};
	const root = mountInPart(t, client.TransportCluster, 'deck.1.transport', props);
	const play = selectorNodes(root).find((n) => n.id === client.ids.transportSelId(1, 'play'));
	// Enter activates the leaf's first enabled control in DOM order (selector-activate, #13708).
	const primary = buttonsIn(play.el).find((b) => !b.hasAttribute('disabled'));
	assert.equal(primary.getAttribute('data-testid'), 'play-deck-1');
	const path = [];
	for (let n = primary; n !== null; n = n.parentNode) path.push(n);
	const click = {
		type: 'click',
		bubbles: true,
		timeStamp: 1,
		metaKey: false,
		ctrlKey: false,
		composedPath: () => path
	};
	primary.dispatchEvent(click);
	await Promise.resolve();
	assert.deepEqual(calls, ['play']);
});

test('KEYB-14374 jog: deck 1 jog is a group of dial, quantize, slip and master-tempo leaves of one control each', (t) => {
	// [if] a deck renders [then] deck.<n>.jog is a group with dial, quantize, slip and master-tempo leaves, each wrapping one control, [else stop].
	const props = {
		deck: loadedDeck(),
		pitchRange: 10,
		pending: false,
		onQuantize: noop,
		onQuantizeGrid: noop,
		onMasterTempo: noop,
		onAutoCue: () => {},
		onSlip: noop
	};
	const root = mountInPart(t, client.JogDial, 'deck.1.jog', props);
	const leaves = selectorNodes(root).filter((n) => n.id !== 'deck.1.jog');
	assert.deepEqual(
		leaves.map((n) => [n.id, n.kind, n.parentId]),
		client.ids.JOG_SEL_CONTROLS.map((c) => [`deck.1.jog.${c}`, 'leaf', 'deck.1.jog'])
	);
	const byId = Object.fromEntries(leaves.map((n) => [n.id.split('.').at(-1), n.el]));
	assert.deepEqual(buttonsIn(byId.dial), [], 'the dial readout holds no button');
	assert.ok(
		walk(byId.dial).some(
			(n) => n.nodeName.toLowerCase() === 'svg' && n.getAttribute('role') === 'img'
		)
	);
	for (const [control, testId] of [
		['quantize', 'quantize-deck-1'],
		['slip', 'slip-deck-1'],
		['master-tempo', 'master-tempo-deck-1'],
		['auto-cue', 'auto-cue-deck-1'],
		['manual-tempo', 'manual-deck-1']
	]) {
		assert.deepEqual(
			buttonsIn(byId[control]).map((b) => b.getAttribute('data-testid')),
			[testId],
			`${control} leaf does not wrap exactly its one button`
		);
	}
});
