/**
 * Viewport placement of the PARITY-02 SOURCE menu ((private path)).
 *
 * AnalysisSourceToggle._show measures the trigger, runs placeFloating with
 * `preferred: 'below'` and `gap: 0` (the flush edge keeps pointerleave from
 * closing the menu), and writes the rounded box as a `left:..px;top:..px`
 * string. These tests replay that calculation for every viewport branch and
 * guard that the toggle still feeds placeFloating the same inputs.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const toggle = readFileSync(
	fileURLToPath(
		new URL('../../src/lib/components/rb/AnalysisSourceToggle.svelte', import.meta.url)
	),
	'utf8'
);

const MARGIN = 8;
const MENU = { width: 190, height: 96 };
const TRIGGER = { width: 70, height: 20 };

let clamp;

before(async () => {
	clamp = await loadTypeScriptModule('src/lib/ui/clamp-to-viewport.ts');
});

/** Mirror of AnalysisSourceToggle._show: trigger rect + menu size + viewport -> menuStyle. */
function sourceMenuStyle(trigger, size, viewport) {
	const box = clamp.placeFloating({ trigger, size, viewport, preferred: 'below', gap: 0 });
	return { box, style: `left:${Math.round(box.x)}px;top:${Math.round(box.y)}px` };
}

// The mirror above is only honest while the toggle passes the same inputs.
test('the toggle places the source menu below its trigger, flush, through placeFloating', () => {
	assert.match(toggle, /import \{ placeFloating \} from '\$lib\/ui\/clamp-to-viewport'/);
	assert.match(toggle, /preferred: 'below'/);
	assert.match(toggle, /gap: 0\b/);
	assert.match(toggle, /viewport: \{ width: window\.innerWidth, height: window\.innerHeight \}/);
	assert.match(
		toggle,
		/menuStyle = `left:\$\{Math\.round\(box\.x\)\}px;top:\$\{Math\.round\(box\.y\)\}px`/
	);
	assert.match(
		toggle,
		/\{ width: 220, height: 160 \}/,
		'unmeasured menu falls back to a fixed size'
	);
});

// Fits below: the menu sits flush under the trigger with no gap.
test('a source menu that fits below its trigger opens flush below it', () => {
	const trigger = { left: 8, top: 4, ...TRIGGER };
	const { box, style } = sourceMenuStyle(trigger, MENU, { width: 790, height: 600 });
	assert.deepEqual(box, { x: 8, y: trigger.top + trigger.height });
	assert.equal(style, 'left:8px;top:24px');
});

// Fractional trigger rects are rounded into whole-pixel offsets.
test('a fractional trigger rect rounds to whole-pixel menu offsets', () => {
	const trigger = { left: 8.6, top: 3.2, width: 70.4, height: 20.1 };
	const { style } = sourceMenuStyle(trigger, MENU, { width: 790, height: 600 });
	assert.equal(style, 'left:9px;top:23px');
});

// Only fits above: the menu flips and its bottom edge meets the trigger's top.
test('a source menu that only fits above its trigger flips above it, flush', () => {
	const viewport = { width: 790, height: 600 };
	const trigger = { left: 120, top: 560, ...TRIGGER };
	const { box, style } = sourceMenuStyle(trigger, MENU, viewport);
	assert.equal(box.x, 120);
	assert.equal(box.y + MENU.height, trigger.top, 'menu bottom meets the trigger top');
	assert.equal(style, 'left:120px;top:464px');
});

// Right viewport edge: the menu is pulled left so its right edge keeps the margin.
test('a trigger near the right viewport edge keeps the whole menu inside the margin', () => {
	const viewport = { width: 790, height: 600 };
	const trigger = { left: 760, top: 4, width: 30, height: 20 };
	const { box, style } = sourceMenuStyle(trigger, MENU, viewport);
	assert.equal(box.x + MENU.width, viewport.width - MARGIN);
	assert.equal(box.y, 24, 'still flush below the trigger');
	assert.equal(style, 'left:592px;top:24px');
});

// Invalid viewports (zero, negative, smaller than the menu) pin to the margin, never off-screen.
test('invalid viewport dimensions pin the source menu to the top-left margin', () => {
	const trigger = { left: 300, top: 200, ...TRIGGER };
	for (const viewport of [
		{ width: 0, height: 0 },
		{ width: -100, height: -50 },
		{ width: 100, height: 60 }
	]) {
		const { box, style } = sourceMenuStyle(trigger, MENU, viewport);
		assert.deepEqual(box, { x: MARGIN, y: MARGIN }, JSON.stringify(viewport));
		assert.equal(style, 'left:8px;top:8px');
	}
});
