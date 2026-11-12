/**
 * Issue #9322: Escape closes the feature-icon list and returns focus to the
 * strip trigger; outside pointerdown closes without moving focus.
 *
 * Extracts the real dismiss handlers from TopBar.svelte (not a reimplementation).
 */
// requirement: FICON-9503
// [if] the DJ presses Escape on the open feature list [then] the list closes and the strip trigger is focused, else stop
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { transformSync } from 'esbuild';
import { compile } from 'svelte/compiler';

const filename = fileURLToPath(
	new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url)
);
const source = readFileSync(filename, 'utf8');

function extractFunction(name) {
	const match = new RegExp(`(async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
	assert.ok(match, `${name} not found in TopBar.svelte`);
	const start = match.index;
	const bodyStart = source.indexOf('{', start);
	let depth = 0;
	let end = bodyStart;
	for (; end < source.length; end++) {
		if (source[end] === '{') depth++;
		else if (source[end] === '}') {
			depth -= 1;
			if (depth === 0) break;
		}
	}
	return source.slice(start, end + 1);
}

function runEscape(open, iconClusterEl, event) {
	const stripped = transformSync(extractFunction('_dismissFeatureMenuOnEscape'), {
		loader: 'ts'
	}).code;
	const harness = new Function(
		'state',
		'iconClusterEl',
		'event',
		`${stripped}
		let featureMenuOpen = state.open;
		_dismissFeatureMenuOnEscape(event);
		return featureMenuOpen;`
	);
	return harness({ open }, iconClusterEl, event);
}

function runOutsidePointer(open, featureIconsEl, event) {
	const stripped = transformSync(extractFunction('_dismissFeatureMenuOnOutsidePointer'), {
		loader: 'ts'
	}).code;
	const harness = new Function(
		'state',
		'featureIconsEl',
		'event',
		`${stripped}
		let featureMenuOpen = state.open;
		_dismissFeatureMenuOnOutsidePointer(event);
		return featureMenuOpen;`
	);
	return harness({ open }, featureIconsEl, event);
}

test('TopBar.svelte still compiles', () => {
	compile(source, { filename, generate: 'client' });
});

test('Escape with menu open and trigger present closes and focuses the strip trigger once', () => {
	let focusCalls = 0;
	const iconClusterEl = {
		focus: () => {
			focusCalls++;
		}
	};
	const out = runEscape(true, iconClusterEl, { key: 'Escape' });
	assert.equal(out, false);
	assert.equal(focusCalls, 1);
});

test('Escape with menu open and no trigger returns false without throwing', () => {
	let outUndefined;
	let outNull;
	assert.doesNotThrow(() => {
		outUndefined = runEscape(true, undefined, { key: 'Escape' });
	});
	assert.equal(outUndefined, false);
	assert.doesNotThrow(() => {
		outNull = runEscape(true, null, { key: 'Escape' });
	});
	assert.equal(outNull, false);
});

test('non-Escape key with menu open leaves open and does not focus', () => {
	let focusCalls = 0;
	const iconClusterEl = {
		focus: () => {
			focusCalls++;
		}
	};
	const out = runEscape(true, iconClusterEl, { key: 'Tab' });
	assert.equal(out, true);
	assert.equal(focusCalls, 0);
});

test('Escape with menu already closed does not focus', () => {
	let focusCalls = 0;
	const iconClusterEl = {
		focus: () => {
			focusCalls++;
		}
	};
	const out = runEscape(false, iconClusterEl, { key: 'Escape' });
	assert.equal(out, false);
	assert.equal(focusCalls, 0);
});

test('outside pointerdown closes the menu', () => {
	globalThis.Node = globalThis.Node ?? class {};
	const outsideTarget = new globalThis.Node();
	const featureIconsEl = { contains: () => false };
	const out = runOutsidePointer(true, featureIconsEl, { target: outsideTarget });
	assert.equal(out, false);
});

test('inside pointerdown leaves the menu open', () => {
	globalThis.Node = globalThis.Node ?? class {};
	const insideTarget = new globalThis.Node();
	const featureIconsEl = { contains: () => true };
	const out = runOutsidePointer(true, featureIconsEl, { target: insideTarget });
	assert.equal(out, true);
});

test('_dismissFeatureMenuOnOutsidePointer source has no .focus (pointer path stays focus-neutral)', () => {
	const fnText = extractFunction('_dismissFeatureMenuOnOutsidePointer');
	assert.doesNotMatch(fnText, /\.focus/, 'outside-pointer dismiss must not call focus');
});
