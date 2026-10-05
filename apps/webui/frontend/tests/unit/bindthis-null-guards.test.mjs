/**
 * Follow-up to topbar-mode-menu-null-bindthis.test.mjs (issue #1320 / PR #1389):
 * Svelte 5 sets an unmounted `bind:this` binding to `null`, not `undefined`, so
 * a guard written as `x === undefined` / `x !== undefined` passes for `null`
 * and the next dereference throws. Issue #1392 lists 14 remaining sites across
 * 9 files. Rather than 14 near-identical test files, this extracts the real
 * guard code out of each component (never a reimplementation) and executes it
 * against a null binding, so each case fails for the same reason the real bug
 * does.
 *
 * Anchors used to locate each guard are chosen from text that survives the
 * fix (the guarded body, not the `=== undefined` / `!== undefined` condition
 * itself), so these tests stay valid both before and after the fix - the
 * whole point of a red-then-green TDD cycle.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { transformSync } from 'esbuild';

import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.Node = globalThis.Node ?? class {};

function readSource(relPath) {
	return readFileSync(
		fileURLToPath(new URL(`../../src/lib/components/rb/${relPath}`, import.meta.url)),
		'utf8'
	);
}

function matchBrace(source, openBraceIdx) {
	let depth = 0;
	for (let i = openBraceIdx; i < source.length; i++) {
		if (source[i] === '{') depth++;
		else if (source[i] === '}') {
			depth -= 1;
			if (depth === 0) return i;
		}
	}
	throw new Error('unbalanced braces starting at ' + openBraceIdx);
}

/** Extract `function NAME(...) { ... }` (optionally `async function`) verbatim. */
function extractFunction(source, file, name) {
	const m = new RegExp(`(async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
	assert.ok(m, `${name} not found in ${file}`);
	const braceStart = source.indexOf('{', m.index);
	const braceEnd = matchBrace(source, braceStart);
	return source.slice(m.index, braceEnd + 1);
}

/** Extract `const NAME = (...) => { ... }` verbatim. */
function extractConstArrow(source, file, name) {
	const marker = `const ${name} = (`;
	const start = source.indexOf(marker);
	assert.notEqual(start, -1, `${marker} not found in ${file}`);
	const braceStart = source.indexOf('{', start);
	const braceEnd = matchBrace(source, braceStart);
	return source.slice(start, braceEnd + 1);
}

/** Extract the arrow body of the `$effect(() => { ... })` whose block contains
 * `anchor` (files here hold more than one `$effect`; the anchor disambiguates). */
function extractEffectArrow(source, file, anchor) {
	const anchorIdx = source.indexOf(anchor);
	assert.notEqual(anchorIdx, -1, `${anchor} not found in ${file}`);
	const header = '$effect(() => {';
	const headerStart = source.lastIndexOf(header, anchorIdx);
	assert.notEqual(headerStart, -1, `${header} not found before anchor in ${file}`);
	const braceStart = headerStart + header.length - 1;
	const braceEnd = matchBrace(source, braceStart);
	return source.slice(headerStart + '$effect('.length, braceEnd + 1); // "() => { ... }"
}

/** Extract the `if (...) { ... }` block whose body contains `anchor`, without
 * caring what the condition says - this is what makes the test survive the
 * fix, which only rewrites the condition, not the guarded body. */
function extractEnclosingIf(source, file, anchor) {
	const anchorIdx = source.indexOf(anchor);
	assert.notEqual(anchorIdx, -1, `${anchor} not found in ${file}`);
	const ifStart = source.lastIndexOf('if (', anchorIdx);
	assert.notEqual(ifStart, -1, `enclosing 'if (' not found before anchor in ${file}`);
	const conditionEnd = matchParen(source, ifStart + 'if '.length);
	const afterCondition = source.slice(conditionEnd + 1).trimStart();
	if (afterCondition.startsWith('return')) {
		// Early-return guard (`if (!el) return;` then the dereference, the shape
		// a355a5066 gave AnalysisDotsPopover._openNow): the guard does not
		// enclose the anchor, so take the guard through the anchor statement so
		// the dereference it protects is still executed.
		return source.slice(ifStart, anchorIdx + anchor.length);
	}
	const braceStart = source.indexOf('{', conditionEnd);
	assert.ok(braceStart < anchorIdx, `'if (' before anchor in ${file} neither returns nor encloses it`);
	const braceEnd = matchBrace(source, braceStart);
	assert.ok(braceEnd > anchorIdx, `'if (' block before anchor in ${file} does not enclose it`);
	return source.slice(ifStart, braceEnd + 1);
}

function matchParen(source, openParenIdx) {
	assert.equal(source[openParenIdx], '(', `expected '(' at ${openParenIdx}`);
	let depth = 0;
	for (let i = openParenIdx; i < source.length; i++) {
		if (source[i] === '(') depth++;
		else if (source[i] === ')') {
			depth -= 1;
			if (depth === 0) return i;
		}
	}
	throw new Error('unbalanced parens starting at ' + openParenIdx);
}

function compile(code) {
	return transformSync(code, { loader: 'ts' }).code;
}

/** Run an extracted named function or const-arrow: binds `freeVars` as
 * closures in scope, then calls the named binding with `callArgs`. */
function runNamed(text, name, freeVars, ...callArgs) {
	const stripped = compile(text);
	const harness = new Function(
		...Object.keys(freeVars),
		'callArgs',
		`${stripped}\nreturn ${name}(...callArgs);`
	);
	return harness(...Object.values(freeVars), callArgs);
}

/** Run an extracted `$effect` arrow body with `freeVars` bound as closures. */
function runEffect(arrowText, freeVars) {
	const stripped = compile(arrowText);
	const harness = new Function(
		...Object.keys(freeVars),
		`const __effect__ = ${stripped};\nreturn __effect__();`
	);
	return harness(...Object.values(freeVars));
}

/** Run an extracted `if (wrapEl ...) { ... }` popover-placement guard: it only
 * ever reads `wrapEl` and assigns a local `popStyle`, so the wrapper supplies
 * both without needing the rest of the component's closures. */
function runWrapElGuard(blockText, wrapEl) {
	const stripped = compile(blockText);
	const harness = new Function('wrapEl', `let popStyle;\n${stripped}\nreturn popStyle;`);
	return harness(wrapEl);
}

// ---------------------------------------------------------------- QuickDrawMenu

test('QuickDrawMenu.svelte: onMenuPointerLeave does not throw once menuEl has unmounted to null', () => {
	const source = readSource('QuickDrawMenu.svelte');
	const fn = extractFunction(source, 'QuickDrawMenu.svelte', 'onMenuPointerLeave');
	let closed = 0;
	// extract harness must bind blend like open/_close; live const is in QuickDrawMenu.svelte
	const freeVars = {
		open: true,
		leaveArmed: true,
		menuEl: null,
		blend: { isActive: () => false },
		_close: () => closed++
	};
	assert.doesNotThrow(() => runNamed(fn, 'onMenuPointerLeave', freeVars, { relatedTarget: new Node() }));
	assert.equal(closed, 1, 'an unmounted menu must still close on an outside pointer leave');
});

test('QuickDrawMenu.svelte: onMount outside-pointerdown handler does not throw once menuEl has unmounted to null', () => {
	const source = readSource('QuickDrawMenu.svelte');
	const fn = extractConstArrow(source, 'QuickDrawMenu.svelte', 'onPointerDown');
	let closed = 0;
	// extract harness must bind blend like open/_close; live const is in QuickDrawMenu.svelte
	const freeVars = {
		open: true,
		menuEl: null,
		blend: { isActive: () => false },
		_close: () => closed++
	};
	assert.doesNotThrow(() => runNamed(fn, 'onPointerDown', freeVars, { target: new Node() }));
	assert.equal(closed, 1, 'an unmounted menu must still close on an outside pointerdown');
});

// --------------------------------------------------------- RefreshAnalysisButton

// 2cc5bd75c (UX-FLOAT-01, #2308) moved popover placement out of onEnter into
// the shared `triggerFloatingAction`, fed `getTrigger: () => wrapEl ?? null`.
// The null guard now lives in two pieces, both exercised for real: the
// component's own getTrigger arrow (extracted verbatim) must map an unmounted
// wrapEl to null, and the real action's place() must return on a null trigger.
// The popover itself is lazily loaded from RefreshAnalysisPopover.svelte
// (issue #3886), which is handed wrapEl as a prop; the button keeps the same
// placement for its load-error box. Both getTrigger arrows are checked.
test('RefreshAnalysisButton.svelte: popover placement does not throw once wrapEl has unmounted to null', async () => {
	let getTrigger;
	for (const file of ['RefreshAnalysisPopover.svelte', 'RefreshAnalysisButton.svelte']) {
		const source = readSource(file);
		const getTriggerText = /getTrigger:\s*(\(\)\s*=>\s*wrapEl\s*\?\?\s*null)/.exec(source);
		assert.ok(getTriggerText, `getTrigger: () => wrapEl ?? null not found in ${file}`);
		getTrigger = new Function('wrapEl', `return ${compile(getTriggerText[1])}`)(null);
		assert.equal(getTrigger(), null, `${file}: an unmounted wrapEl must reach the action as null, not undefined`);
		// A wrapEl prop not yet bound arrives as undefined; it must also be null.
		const unbound = new Function('wrapEl', `return ${compile(getTriggerText[1])}`)(undefined);
		assert.equal(unbound(), null, `${file}: an unbound wrapEl must reach the action as null`);
	}

	const { triggerFloatingAction } = await loadTypeScriptModule('src/lib/ui/clamp-to-viewport.ts');
	const saved = {
		ResizeObserver: globalThis.ResizeObserver,
		window: globalThis.window,
		requestAnimationFrame: globalThis.requestAnimationFrame
	};
	globalThis.ResizeObserver = FakeResizeObserver;
	globalThis.window = { innerWidth: 800, innerHeight: 600, addEventListener() {}, removeEventListener() {} };
	globalThis.requestAnimationFrame = (cb) => cb();
	try {
		const node = { offsetWidth: 100, offsetHeight: 40, style: {} };
		let action;
		assert.doesNotThrow(() => {
			action = triggerFloatingAction(node, { getTrigger, preferred: 'below', gap: 4 });
			action.update({ getTrigger, preferred: 'below', gap: 4 });
		});
		assert.deepEqual(node.style, {}, 'a null trigger must leave the popover unplaced');
		action.destroy();
	} finally {
		Object.assign(globalThis, saved);
	}
});

// ------------------------------------------------------------ AnalysisDotsPopover

test('AnalysisDotsPopover.svelte: _openNow popover-placement guard does not throw once wrapEl has unmounted to null', () => {
	const source = readSource('browser/AnalysisDotsPopover.svelte');
	const block = extractEnclosingIf(
		source,
		'browser/AnalysisDotsPopover.svelte',
		'wrapEl.getBoundingClientRect();'
	);
	assert.doesNotThrow(() => runWrapElGuard(block, null));
});

test('AnalysisDotsPopover.svelte: onFocusOut does not throw once wrapEl has unmounted to null', () => {
	const source = readSource('browser/AnalysisDotsPopover.svelte');
	const fn = extractFunction(source, 'browser/AnalysisDotsPopover.svelte', 'onFocusOut');
	assert.doesNotThrow(() =>
		runNamed(fn, 'onFocusOut', { wrapEl: null, hovered: true }, { relatedTarget: new Node() })
	);
});

// ----------------------------------------------------------------- AutoPlayExplainer

test('AutoPlayExplainer.svelte: _place does not throw once wrapEl has unmounted to null', () => {
	const source = readSource('browser/AutoPlayExplainer.svelte');
	const fn = extractFunction(source, 'browser/AutoPlayExplainer.svelte', '_place');
	// `columnExplainerStyle` only needs to exist as a binding: the guard must
	// return before the real geometry helper is ever reached.
	const freeVars = { wrapEl: null, panelEl: {}, columnExplainerStyle: undefined };
	assert.doesNotThrow(() => runNamed(fn, '_place', freeVars));
});

test('AutoPlayExplainer.svelte: position-tracking effect does not call _place once panelEl has unmounted to null', () => {
	const source = readSource('browser/AutoPlayExplainer.svelte');
	const arrow = extractEffectArrow(source, 'browser/AutoPlayExplainer.svelte', '_place();');
	let placedUnsafely = false;
	// Stands in for the real `_place` (covered separately above) so this test
	// isolates the EFFECT's own guard rather than `_place`'s.
	const _place = () => {
		if (panelElStub === null) placedUnsafely = true;
	};
	const panelElStub = null;
	const windowStub = { addEventListener() {}, removeEventListener() {} };
	assert.doesNotThrow(() =>
		runEffect(arrow, { open: true, panelEl: panelElStub, _place, window: windowStub })
	);
	assert.equal(placedUnsafely, false, 'the effect must not call _place once panelEl has unmounted to null');
});

// -------------------------------------------------------------------- ControlExplainer

test('ControlExplainer.svelte: _place does not throw once wrapEl has unmounted to null', () => {
	const source = readSource('deck/ControlExplainer.svelte');
	const fn = extractFunction(source, 'deck/ControlExplainer.svelte', '_place');
	assert.doesNotThrow(() => runNamed(fn, '_place', { wrapEl: null }));
});

// ------------------------------------------------------------------------- PitchFader

// A no-op stand-in so `new ResizeObserver(...)` (unavailable in node:test) does
// not fail these effects for the wrong reason before they ever reach the
// null-element dereference under test.
class FakeResizeObserver {
	observe() {}
	disconnect() {}
}

test('PitchFader.svelte: track-height effect does not throw once trackEl has unmounted to null', () => {
	const source = readSource('deck/PitchFader.svelte');
	const arrow = extractEffectArrow(
		source,
		'deck/PitchFader.svelte',
		'trackH = el.getBoundingClientRect().height;'
	);
	assert.doesNotThrow(() =>
		runEffect(arrow, { trackEl: null, ResizeObserver: FakeResizeObserver })
	);
});

// ----------------------------------------------------------------------------- VFader

test('VFader.svelte: _valueFromEvent does not throw and falls back to the current value once trackEl has unmounted to null', () => {
	const source = readSource('mixer/VFader.svelte');
	const fn = extractFunction(source, 'mixer/VFader.svelte', '_valueFromEvent');
	let result;
	assert.doesNotThrow(() => {
		result = runNamed(fn, '_valueFromEvent', { trackEl: null, value: 0.5 }, { clientY: 100 });
	});
	assert.equal(result, 0.5, 'an unmounted track must fall back to the current value, not throw');
});

test('VFader.svelte: track-height effect does not throw once trackEl has unmounted to null', () => {
	const source = readSource('mixer/VFader.svelte');
	const arrow = extractEffectArrow(
		source,
		'mixer/VFader.svelte',
		'trackH = Math.max(1, Math.round(el.getBoundingClientRect().height));'
	);
	assert.doesNotThrow(() =>
		runEffect(arrow, { trackEl: null, ResizeObserver: FakeResizeObserver })
	);
});

// ---------------------------------------------------------------------------- WaveRow

test('WaveRow.svelte: palette effect does not throw once canvasEl has unmounted to null', () => {
	const source = readSource('wave/WaveRow.svelte');
	const arrow = extractEffectArrow(source, 'wave/WaveRow.svelte', 'palette = readPalette(el);');
	// The real readPalette needs getComputedStyle, unavailable outside a
	// browser, so this stand-in dereferences its argument the same way -
	// the guard must return before either ever runs.
	const readPalette = (el) => el.tagName;
	assert.doesNotThrow(() =>
		runEffect(arrow, { canvasEl: null, readPalette, ResizeObserver: FakeResizeObserver })
	);
});

// ----------------------------------------------------------------------- PreviewStrip

test('PreviewStrip.svelte: draw effect does not call _draw once canvas has unmounted to null', () => {
	const source = readSource('browser/PreviewStrip.svelte');
	const arrow = extractEffectArrow(source, 'browser/PreviewStrip.svelte', 'strip !== null && revealed');
	let drawnWithNull = false;
	const _draw = (el) => {
		if (el === null) drawnWithNull = true;
	};
	assert.doesNotThrow(() =>
		runEffect(arrow, {
			canvas: null,
			strip: {},
			revealed: true,
			vocals: null,
			duration_ms: 0,
			dpr: 1,
			uiPrefs: { waveform_design: 'tri-band' },
			_draw
		})
	);
	assert.equal(drawnWithNull, false, 'the effect must not call _draw once canvas has unmounted to null');
});
