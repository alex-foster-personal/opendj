/**
 * K4 / #9396: selector ring helper, styles and SSR.
 *
 * [if] a node is selected [then] the ring box equals the node rect plus 2px per side [else stop].
 * [if] prefers-reduced-motion: reduce [then] the stylesheet sets transition none on the ring [else stop].
 * [if] the selector is cleared [then] the ring element is not in the rendered markup [else stop].
 * [if] motion is allowed [then] only transform and opacity appear in transition declarations [else stop].
 *
 * A pass here does not prove computed styles in a browser; it reads the component source and SSR HTML.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

import {
	SELECTOR_RING_INSET_PX,
	insetSelectorRingRect,
	selectorRingBorderStyle,
	selectorRingTransform
} from '../../src/lib/keyboard/selector-ring.ts';

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
const COMPONENT = read('../../src/lib/components/keyboard/SelectorRing.svelte');

/** Body of the first `selector {` rule in a component's `<style>`. */
function ruleBody(source, selector) {
	const style = source.slice(source.indexOf('<style'));
	const at = style.indexOf(`${selector} {`);
	assert.notEqual(at, -1, `rule ${selector} not found`);
	return style.slice(at, style.indexOf('}', at));
}

function transitionPropertyNames(transitionValue) {
	const normalized = transitionValue.replace(/\s+/g, ' ').trim();
	const parts = normalized.split(',').map((p) => p.trim());
	const names = [];
	for (const part of parts) {
		const first = part.split(/\s+/)[0];
		if (first) names.push(first);
	}
	return names;
}

function collectTransitionDeclarations(cssText) {
	const out = [];
	const re = /\btransition(?:-property)?\s*:\s*([^;{}]+);/g;
	let m;
	while ((m = re.exec(cssText)) !== null) {
		out.push(m[1].trim());
	}
	return out;
}

test('SELECTOR_RING_INSET_PX is 2', () => {
	assert.equal(SELECTOR_RING_INSET_PX, 2);
});

test('insetSelectorRingRect outsets the box by 2px per edge', () => {
	assert.deepEqual(insetSelectorRingRect({ x: 10, y: 20, width: 100, height: 50 }), {
		x: 8,
		y: 18,
		width: 104,
		height: 54
	});
});

test('selectorRingTransform matches the inset position', () => {
	const box = insetSelectorRingRect({ x: 10, y: 20, width: 100, height: 50 });
	assert.equal(selectorRingTransform(box), 'translate3d(8px, 18px, 0)');
});

test('selectorRingBorderStyle is dashed for sections and solid otherwise', () => {
	assert.equal(selectorRingBorderStyle('section'), 'dashed');
	assert.equal(selectorRingBorderStyle('group'), 'solid');
	assert.equal(selectorRingBorderStyle('leaf'), 'solid');
});

test('insetSelectorRingRect throws RangeError on non-finite values', () => {
	assert.throws(() => insetSelectorRingRect({ x: NaN, y: 0, width: 1, height: 1 }), RangeError);
});

test('insetSelectorRingRect throws RangeError on negative width', () => {
	assert.throws(() => insetSelectorRingRect({ x: 0, y: 0, width: -1, height: 1 }), RangeError);
});

test('base selector-ring rule uses VS Code-style accent border and no shadow', () => {
	const body = ruleBody(COMPONENT, '.selector-ring');
	assert.match(body, /border:\s*1px solid var\(--rb-accent\)/);
	assert.match(body, /box-shadow:\s*none/);
	assert.match(body, /box-sizing:\s*border-box/);
});

test('section nodes use a dashed border', () => {
	const body = ruleBody(COMPONENT, '.selector-ring.section');
	assert.match(body, /border-style:\s*dashed/);
});

test('motion transitions only transform and opacity outside reduced-motion block', () => {
	const style = COMPONENT.slice(COMPONENT.indexOf('<style'), COMPONENT.lastIndexOf('</style>'));
	const mediaStart = style.indexOf('@media (prefers-reduced-motion: reduce)');
	assert.notEqual(mediaStart, -1);
	const mobileFree = style.slice(0, mediaStart);
	const allowed = new Set(['transform', 'opacity']);
	const decls = collectTransitionDeclarations(mobileFree);
	assert.ok(decls.length > 0, 'expected at least one transition declaration');
	for (const decl of decls) {
		for (const name of transitionPropertyNames(decl)) {
			assert.ok(
				allowed.has(name),
				`transition property "${name}" must be transform or opacity, got: ${decl}`
			);
		}
	}
});

test('reduced-motion block removes transition on .selector-ring', () => {
	const style = COMPONENT.slice(COMPONENT.indexOf('<style'), COMPONENT.lastIndexOf('</style>'));
	const mediaStart = style.indexOf('@media (prefers-reduced-motion: reduce)');
	const mediaBody = style.slice(mediaStart);
	const ringRuleStart = mediaBody.indexOf('.selector-ring {');
	assert.notEqual(ringRuleStart, -1);
	const block = mediaBody.slice(ringRuleStart, mediaBody.indexOf('}', ringRuleStart) + 1);
	assert.match(block, /transition:\s*none|transition-duration:\s*0s/);
});

const ENTRY = [
	"export { default as Ring } from '$lib/components/keyboard/SelectorRing.svelte';",
	"export { clearSelectorRingTarget, setSelectorRingTarget, selectorRing } from '$lib/keyboard/selector-ring-state.svelte.ts';",
	"export { render } from 'svelte/server';"
].join('\n');

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule(ENTRY);
});

afterEach(() => {
	ssr.clearSelectorRingTarget();
});

test('SSR: cleared selector renders no ring element', () => {
	ssr.clearSelectorRingTarget();
	const html = ssr.render(ssr.Ring).body;
	assert.equal(html.includes('data-testid="selector-ring"'), false, html);
});

test('SSR: set target renders inset box and transform', () => {
	ssr.setSelectorRingTarget({
		id: 'a',
		rect: { x: 10, y: 20, width: 100, height: 50 },
		kind: 'leaf'
	});
	const html = ssr.render(ssr.Ring).body;
	assert.match(html, /data-testid="selector-ring"/);
	assert.match(html, /translate3d\(8px,\s*18px,\s*0\)/);
	assert.match(html, /width:\s*104px/);
	assert.match(html, /height:\s*54px/);
});

test('SSR: section kind adds section class', () => {
	ssr.setSelectorRingTarget({
		id: 'b',
		rect: { x: 0, y: 0, width: 10, height: 10 },
		kind: 'section'
	});
	const html = ssr.render(ssr.Ring).body;
	assert.match(html, /class="[^"]*\bsection\b[^"]*selector-ring|selector-ring[^"]*\bsection\b/);
});

test('SSR: clear after set removes ring', () => {
	ssr.setSelectorRingTarget({
		id: 'c',
		rect: { x: 1, y: 2, width: 3, height: 4 },
		kind: 'leaf'
	});
	assert.match(ssr.render(ssr.Ring).body, /data-testid="selector-ring"/);
	ssr.clearSelectorRingTarget();
	assert.equal(ssr.render(ssr.Ring).body.includes('data-testid="selector-ring"'), false);
});
