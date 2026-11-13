/**
 * Mixer and browser compact tiers by container query (SKINLAYOUT-13413, issue #13413).
 *
 * Regression lines:
 * - if .rb-mixer is not a named inline-size container mixer then broken
 * - if .rb-browser is not a named inline-size container browser then broken
 * - if a mixer or browser tier uses @media or a second breakpoint then broken
 * - if a compact threshold matches the default width (248 px mixer column) then broken
 * - if a compact tier hides a control or scales a font then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { MIXER_COLUMN_PX } from '../../src/lib/rb/mixer-width.ts';

const ROOT = fileURLToPath(new URL('../../', import.meta.url));
const read = (rel) => readFileSync(`${ROOT}${rel}`, 'utf8');

const mixer = read('src/lib/components/rb/Mixer.svelte');
const browser = read('src/lib/components/rb/BrowserPanel.svelte');

/** Body of the first brace block whose opening `{` follows `index`. */
function blockAt(source, index) {
	let i = source.indexOf('{', index);
	assert(i >= 0, 'no opening brace');
	const start = i + 1;
	let depth = 0;
	for (; i < source.length; i++) {
		if (source[i] === '{') depth++;
		else if (source[i] === '}') {
			depth--;
			if (depth === 0) return source.slice(start, i);
		}
	}
	throw new Error('unclosed block');
}

/** Body of the first rule whose selector is exactly `selector`. */
function ruleBody(source, selector) {
	const re = new RegExp(`\\n\\t${selector.replace('.', '\\.')}\\s*\\{`);
	const m = re.exec(source);
	assert(m, `${selector} rule missing`);
	return blockAt(source, m.index);
}

/** Every `@container <name> (max-width: Npx) { ... }` rule as { width, body }. */
function containerTiers(source, name) {
	const re = new RegExp(`@container ${name} \\(max-width: (\\d+)px\\)\\s*\\{`, 'g');
	return [...source.matchAll(re)].map((m) => ({
		width: Number(m[1]),
		body: blockAt(source, m.index)
	}));
}

/** Every `@media (...)` condition string in source, in file order. */
function mediaQueries(source) {
	return [...source.matchAll(/@media\s*\(([^)]+)\)/g)].map((m) => `(${m[1]})`);
}

test('.rb-mixer and .rb-browser declare named inline-size containers', () => {
	const mixerRule = ruleBody(mixer, '.rb-mixer');
	assert.match(mixerRule, /container-type:\s*inline-size/);
	assert.match(mixerRule, /container-name:\s*mixer/);
	const browserRule = ruleBody(browser, '.rb-browser');
	assert.match(browserRule, /container-type:\s*inline-size/);
	assert.match(browserRule, /container-name:\s*browser/);
});

test('one compact breakpoint each, below the default region width', () => {
	const mixerTiers = containerTiers(mixer, 'mixer');
	assert.deepEqual(
		mixerTiers.map((t) => t.width),
		[229]
	);
	// .rb-mixer pads 6 px each side, so its content box is the column minus 12.
	assert.ok(mixerTiers[0].width < MIXER_COLUMN_PX - 12, 'mixer tier matches the default column');
	const browserTiers = containerTiers(browser, 'browser');
	assert.deepEqual(
		browserTiers.map((t) => t.width),
		[599]
	);
	assert.ok(browserTiers[0].width < 1280 / 2, 'browser tier matches a 1280 px window');
});

test('compact tiers wrap controls and never hide them or scale fonts', () => {
	const tiers = [...containerTiers(mixer, 'mixer'), ...containerTiers(browser, 'browser')];
	for (const { body } of tiers) {
		assert.match(body, /flex-wrap:\s*wrap/);
		assert.doesNotMatch(body, /display:\s*none|visibility:\s*hidden|font-size|transform|zoom/);
	}
	assert.match(containerTiers(mixer, 'mixer')[0].body, /\.mixer-view-row,\s*\.xfade-row\s*\{/);
	assert.match(
		containerTiers(browser, 'browser')[0].body,
		/\.pane-header,\s*\.header-controls-cluster\s*\{/
	);
});

test('@media baseline unchanged in touched files', () => {
	assert.deepEqual(mediaQueries(mixer), ['(prefers-reduced-motion: reduce)']);
	assert.deepEqual(mediaQueries(browser), [
		'(max-height: 799px)',
		'(prefers-reduced-motion: reduce)'
	]);
});
