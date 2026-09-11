/**
 * TopBar's mode-menu and AutoPlay-menu window handlers guarded their
 * `bind:this` targets against `undefined` only. Svelte 5 sets an unmounted
 * `bind:this` binding to `null`, not `undefined`, so the guard passed for a
 * `null` element and the next dereference threw - a window-error on
 * /performance (issue #1320): "Cannot read properties of null (reading
 * 'open')" at `_dismissModeMenuOnOutsidePointer`.
 *
 * These tests extract the real function source out of TopBar.svelte (not a
 * reimplementation) and execute it directly against a null bound element, so
 * a regression here fails for the same reason the real bug did.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { transformSync } from 'esbuild';
import { compile } from 'svelte/compiler';

const filename = fileURLToPath(new URL('../../src/lib/components/rb/TopBar.svelte', import.meta.url));
const source = readFileSync(filename, 'utf8');

function extractFunction(name) {
	const start = source.indexOf(`function ${name}(`);
	assert.notEqual(start, -1, `${name} not found in TopBar.svelte`);
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

/** Run one extracted TopBar function with `boundVarName` bound to
 * `boundValue` in its closure, exactly as the real component's module scope
 * supplies `modePickerEl` / `autoPlayWrapEl` to it. */
function runExtracted(fnName, boundVarName, boundValue, ...args) {
	const stripped = transformSync(extractFunction(fnName), { loader: 'ts' }).code;
	const harness = new Function(boundVarName, 'callArgs', `${stripped}\nreturn ${fnName}(...callArgs);`);
	return harness(boundValue, args);
}

test('TopBar.svelte still compiles', () => {
	compile(source, { filename, generate: 'client' });
});

test('_dismissModeMenuOnOutsidePointer does not throw once modePickerEl has unmounted to null', () => {
	assert.doesNotThrow(() =>
		runExtracted('_dismissModeMenuOnOutsidePointer', 'modePickerEl', null, { target: null })
	);
});

test('_dismissModeMenuOnEscape does not throw once modePickerEl has unmounted to null', () => {
	assert.doesNotThrow(() =>
		runExtracted('_dismissModeMenuOnEscape', 'modePickerEl', null, { key: 'Escape' })
	);
});

test('_placeModeMenu does not throw once modePickerEl has unmounted to null', () => {
	assert.doesNotThrow(() => runExtracted('_placeModeMenu', 'modePickerEl', null));
});

test('_placeAutoPlayMenu does not throw once autoPlayWrapEl has unmounted to null', () => {
	assert.doesNotThrow(() => runExtracted('_placeAutoPlayMenu', 'autoPlayWrapEl', null));
});

test('_dismissModeMenuOnOutsidePointer still dismisses on a genuine outside pointerdown', () => {
	globalThis.Node = globalThis.Node ?? class {};
	const outsideTarget = new globalThis.Node();
	const modePickerEl = { open: true, contains: () => false };
	runExtracted('_dismissModeMenuOnOutsidePointer', 'modePickerEl', modePickerEl, { target: outsideTarget });
	assert.equal(modePickerEl.open, false, 'a real outside pointerdown must still close the open menu');
});
