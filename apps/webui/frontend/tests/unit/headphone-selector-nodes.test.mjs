/**
 * requirement: KEYB-14402 (#14334, epic #9372): the mixer headphone cluster is a selector group with one leaf per control.
 *
 * The real HeadphoneCluster.svelte is client-compiled and mounted on the node DOM shim, so its
 * real `selectorNode` actions run and the test reads the nodes the page would register.
 *
 * Regression lines:
 * - if mixer.headphones is not a group holding the mix, level, io and midi leaves then broken
 * - if the mix or level leaf does not carry its knob registry id as data-sel-knob then broken
 * - if Enter on the io leaf presses anything but the SHOW AUDIO I/O button then broken
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
	name: 'headphone-selector-client',
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
	// knob-control.svelte and <svelte:window> listen on window, which the shim does not provide.
	globalThis.addEventListener ??= () => {};
	globalThis.removeEventListener ??= () => {};
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
export { default as HeadphoneCluster } from '$lib/components/rb/mixer/HeadphoneCluster.svelte';
export { _defaultHeadphones } from '$lib/player/state.svelte';
export { ioSurface, closeIoView } from '$lib/rb/io-surface.svelte';
export { knobId } from '$lib/rb/knob-control.svelte';
export * as ids from '$lib/components/rb/mixer/headphone-selector-ids';`,
			resolveDir: FRONTEND_ROOT,
			loader: 'js',
			sourcefile: 'headphone-selector-entry.mjs'
		},
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			'import.meta.env.DEV': 'false',
			'import.meta.env.VITE_API_BASE': JSON.stringify('https://headphone-selector.example.test')
		},
		format: 'esm',
		logLevel: 'silent',
		platform: 'browser',
		conditions: ['browser'],
		plugins: [viteUrlSuffixPlugin, svelteClientPlugin],
		target: 'es2022',
		write: false
	});
	client = await importBundledSource(result.outputFiles[0].text, 'headphone-selector-client');
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

/** Controls Enter can operate (selector-activate's SELECTOR_CONTROL rule, #13708), in DOM order. */
const controlsIn = (el) =>
	walk(el).filter((n) => {
		const tag = n.nodeName.toLowerCase();
		if (['button', 'input', 'select', 'textarea'].includes(tag)) return !n.hasAttribute('disabled');
		return n.getAttribute('role') === 'slider' || n.getAttribute('role') === 'button';
	});

const noop = () => {};

/** Mount the cluster inside a stand-in for Mixer.svelte's hp-row, registered as a group. */
function mountCluster(t) {
	const group = document.createElement('div');
	group.setAttribute('data-sel-node', 'mixer.headphones');
	group.setAttribute('data-sel-kind', 'group');
	document.body.appendChild(group);
	const props = {
		state: client._defaultHeadphones(),
		onmix: noop,
		onlevel: noop,
		ondelay: noop,
		onrefresh: async () => ({ ok: true }),
		onacquire: noop,
		onselect: noop,
		onmaster: noop,
		oninput: noop,
		onmode: noop,
		oncalibrate: noop,
		onAlignmentMode: noop,
		testrun: async (work) => await work(() => true, new AbortController().signal)
	};
	const instance = client.mount(client.HeadphoneCluster, { target: group, props });
	client.flushSync();
	t.after(() => {
		client.unmount(instance);
		group.remove();
	});
	return group;
}

test('KEYB-14402 group: mixer.headphones is a group of mix, level, io and midi leaves', (t) => {
	// [if] the mixer renders [then] mixer.headphones is a group whose children are the mix, level, io and midi leaves, [else stop].
	const root = mountCluster(t);
	const leaves = selectorNodes(root).filter((n) => n.id !== 'mixer.headphones');
	assert.deepEqual(
		leaves.map((n) => [n.id, n.kind, n.parentId, n.disabled]),
		client.ids.HEADPHONE_SEL_CONTROLS.map((c) => [
			`mixer.headphones.${c}`,
			'leaf',
			'mixer.headphones',
			false
		])
	);
	const labels = leaves.map((n) => controlsIn(n.el).map((c) => c.getAttribute('aria-label')));
	assert.deepEqual(labels, [
		['Headphone CUE to MASTER mix'],
		['Headphone cue gain'],
		['SHOW AUDIO I/O'],
		['Open MIDI panel']
	]);
});

test('KEYB-14402 knob leaves: mix and level carry their knob registry id as data-sel-knob', (t) => {
	// [if] the mix or level leaf renders [then] it carries data-sel-knob equal to that knob's registry id, [else stop].
	const root = mountCluster(t);
	const byId = Object.fromEntries(selectorNodes(root).map((n) => [n.id, n.el]));
	assert.equal(
		byId['mixer.headphones.mix'].getAttribute('data-sel-knob'),
		client.knobId('hp', 'hp-mix')
	);
	assert.equal(
		byId['mixer.headphones.level'].getAttribute('data-sel-knob'),
		client.knobId('hp', 'hp-level')
	);
	assert.equal(client.knobId('hp', 'hp-mix'), 'hp:hp-mix');
	for (const id of ['mixer.headphones.io', 'mixer.headphones.midi']) {
		assert.equal(byId[id].hasAttribute('data-sel-knob'), false, `${id} must not name a knob`);
	}
});

test('KEYB-14402 io leaf: Enter on the io leaf presses SHOW AUDIO I/O and opens the I/O view', async (t) => {
	// [if] the io leaf is selected and Enter is pressed [then] the SHOW AUDIO I/O button is the control activated, [else stop].
	const root = mountCluster(t);
	const io = selectorNodes(root).find((n) => n.id === client.ids.headphoneSelId('io'));
	// Enter activates the leaf's first enabled control in DOM order (selector-activate, #13708).
	const [primary] = controlsIn(io.el);
	assert.equal(primary.getAttribute('aria-label'), 'SHOW AUDIO I/O');
	assert.equal(client.ioSurface.open, false);
	const path = [];
	for (let n = primary; n !== null; n = n.parentNode) path.push(n);
	primary.dispatchEvent({
		type: 'click',
		bubbles: true,
		timeStamp: 1,
		metaKey: false,
		ctrlKey: false,
		composedPath: () => path
	});
	// Read the store before Svelte renders the panel: its device selects need more DOM than the shim has.
	const opened = client.ioSurface.open;
	client.closeIoView();
	client.flushSync();
	assert.equal(opened, true, 'Enter on the io leaf did not open the audio I/O view');
});
