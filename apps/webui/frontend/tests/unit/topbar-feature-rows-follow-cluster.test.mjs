/**
 * Issue #9330: feature-icon overflow rows and menu use fixed coordinates from
 * the cluster rect; the placement effect must re-run when a sibling in the
 * top bar resizes and moves the cluster without resizing the cluster box.
 *
 * requirement: FICON-9516
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { transformSync } from 'esbuild';
import { compile } from 'svelte/compiler';

const filename = fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url));
const source = readFileSync(filename, 'utf8');

function matchBrace(openBraceIdx) {
	let depth = 0;
	for (let i = openBraceIdx; i < source.length; i++) {
		if (source[i] === '{') depth++;
		else if (source[i] === '}') {
			depth -= 1;
			if (depth === 0) return i;
		}
	}
	throw new Error('unbalanced braces');
}

/** Extract `function NAME(...) { ... }` verbatim. */
function extractFunction(name) {
	const m = new RegExp(`(async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
	assert.ok(m, `${name} not found`);
	const braceStart = source.indexOf('{', m.index);
	const braceEnd = matchBrace(braceStart);
	return source.slice(m.index, braceEnd + 1);
}

/** Extract the `$effect(() => { ... })` arrow whose block contains `anchor`. */
function extractEffectArrow(anchor) {
	const anchorIdx = source.indexOf(anchor);
	assert.notEqual(anchorIdx, -1, `${anchor} not found`);
	const header = '$effect(() => {';
	const headerStart = source.lastIndexOf(header, anchorIdx);
	assert.notEqual(headerStart, -1, `${header} not found before anchor`);
	const braceStart = headerStart + header.length - 1;
	const braceEnd = matchBrace(braceStart);
	return source.slice(headerStart + '$effect('.length, braceEnd + 1);
}

function compileTs(code) {
	return transformSync(code, { loader: 'ts' }).code;
}

test('TopBar.svelte still compiles', () => {
	compile(source, { filename, generate: 'client' });
});

test('placement effect observes cluster, top-bar row, and row children; ResizeObserver callback re-places after cluster rect moves; teardown disconnects observers and window resize', () => {
	const placeExtra = compileTs(extractFunction('_placeFeatureExtra'));
	const effectArrow = compileTs(extractEffectArrow('_placeFeatureExtra();'));

	const clusterRect = { left: 200, bottom: 40, width: 10, height: 28, top: 12, right: 210 };
	const cluster = {
		getBoundingClientRect() {
			return { ...clusterRect };
		}
	};
	const sibling = {
		getBoundingClientRect() {
			return { left: 0, bottom: 40, width: 100, height: 28, top: 12, right: 100 };
		}
	};
	const row = {
		children: [sibling, cluster]
	};

	const resizeListeners = [];
	const fakeWindow = {
		addEventListener(type, fn) {
			if (type === 'resize') resizeListeners.push(fn);
		},
		removeEventListener(type, fn) {
			const i = resizeListeners.indexOf(fn);
			if (i !== -1) resizeListeners.splice(i, 1);
		}
	};

	/** @type {{ instance?: InstanceType<typeof RecordingResizeObserver> }} */
	const roBox = {};
	/** @type {{ instance?: InstanceType<typeof RecordingMutationObserver> }} */
	const moBox = {};

	class RecordingResizeObserver {
		/** @param {() => void} cb */
		constructor(cb) {
			/** @type {() => void} */
			this._cb = cb;
			/** @type {unknown[]} */
			this.targets = [];
			this.disconnected = false;
			roBox.instance = this;
		}
		/** @param {unknown} target */
		observe(target) {
			this.targets.push(target);
		}
		disconnect() {
			this.disconnected = true;
		}
	}

	class RecordingMutationObserver {
		/** @param {() => void} cb */
		constructor(cb) {
			/** @type {() => void} */
			this._cb = cb;
			/** @type {unknown | null} */
			this.observed = null;
			/** @type {MutationObserverInit | null} */
			this.options = null;
			this.disconnected = false;
			moBox.instance = this;
		}
		/** @param {unknown} target @param {MutationObserverInit} options */
		observe(target, options) {
			this.observed = target;
			this.options = options;
		}
		disconnect() {
			this.disconnected = true;
		}
	}

	const harness = new Function(
		'_cluster',
		'_row',
		'featureIconSingleClick',
		'ResizeObserver',
		'MutationObserver',
		'window',
		`const iconClusterEl = _cluster;
const topBarRowEl = _row;
${placeExtra}
let featureExtraStyle = 'visibility:hidden';
let featureMenuOpen = false;
async function _placeFeatureMenu() {}
const __effect__ = ${effectArrow};
const cleanup = __effect__();
return { cleanup, get extraStyle() { return featureExtraStyle; } };`
	);

	const out = harness(
		cluster,
		row,
		() => false,
		RecordingResizeObserver,
		RecordingMutationObserver,
		fakeWindow
	);

	const ro = roBox.instance;
	assert.ok(ro, 'ResizeObserver was constructed');
	assert.equal(resizeListeners.length, 1, 'window resize listener registered');

	assert.ok(ro.targets.includes(cluster), 'observed icon cluster');
	assert.ok(ro.targets.includes(row), 'observed top-bar row');
	assert.ok(ro.targets.includes(sibling), 'observed a row child (sibling before cluster)');

	const mo = moBox.instance;
	assert.ok(mo, 'MutationObserver was constructed');
	assert.strictEqual(mo.observed, row, 'MutationObserver observes the row');
	assert.deepEqual(mo.options, { childList: true });

	assert.match(out.extraStyle, /^left:200px;/, 'initial placement uses cluster left');

	clusterRect.left = 260;
	ro._cb();
	assert.match(out.extraStyle, /^left:260px;/, 'callback re-reads cluster rect after sibling-driven shift');

	assert.equal(typeof out.cleanup, 'function');
	const cleanup = out.cleanup;
	cleanup();
	assert.equal(ro.disconnected, true);
	assert.equal(mo.disconnected, true);
	assert.equal(resizeListeners.length, 0);
});
