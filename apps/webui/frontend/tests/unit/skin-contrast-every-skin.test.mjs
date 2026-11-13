/**
 * Issue #13873: every UI_SKIN_CHOICES skin's theme.css token block, merged
 * over its base scheme, is run through color-contrast.ts PAIRINGS. Known
 * violations live in fixtures/skin-contrast-baseline.json and may only shrink.
 *
 * Regression lines:
 * - if a non-default skin has no parsable token block then the test fails naming it
 * - if a skin gains a violation not in the baseline then the test fails naming
 *   the skin, pairing and ratio
 * - if a baseline entry no longer fails then the test asks for it to be removed
 * - if a block sets --rb-text equal to --rb-bg then parser plus validator report it
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const THEME_CSS = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url)),
	'utf8'
);
const BASELINE = JSON.parse(
	readFileSync(new URL('./fixtures/skin-contrast-baseline.json', import.meta.url), 'utf8')
).violations;

let cc;
let sk;
let skins;
before(async () => {
	cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
	sk = await loadTypeScriptModule('src/lib/rb/skin-token-blocks.ts');
	skins = (await loadTypeScriptModule('src/lib/rb/ui-skin.ts')).UI_SKIN_CHOICES;
});

function violationsFor(skin) {
	return cc.validateScheme(sk.skinTokens(THEME_CSS, skin)).map((v) => ({
		key: `${skin}: ${v.pairing.fg} on ${v.pairing.bg}`,
		ratio: v.ratio,
		detail: v.ratio !== undefined ? `${v.ratio.toFixed(2)}:1 < ${v.required}:1` : v.reason
	}));
}

test('every non-default skin has a parsable token block', () => {
	const missing = skins.filter((s) => sk.skinTokens(THEME_CSS, s) === null);
	assert.deepEqual(
		missing,
		[],
		`skins with no parsable theme.css token block: ${missing.join(', ')}`
	);
});

test('a skin with no token block is reported as null, default never is', () => {
	assert.equal(sk.skinTokens(THEME_CSS, 'no-such-skin'), null);
	assert.notEqual(sk.skinTokens(THEME_CSS, 'default'), null);
});

test('no skin gains a contrast violation outside the baseline', () => {
	const fresh = skins
		.flatMap(violationsFor)
		.filter((v) => !BASELINE.includes(v.key))
		.map((v) => `${v.key} (${v.detail})`);
	assert.deepEqual(fresh, [], `new skin contrast violations:\n${fresh.join('\n')}`);
});

test('every baseline entry still fails (the baseline may only shrink)', () => {
	const live = new Set(skins.flatMap(violationsFor).map((v) => v.key));
	const stale = BASELINE.filter((k) => !live.has(k));
	assert.deepEqual(stale, [], `baseline entries now pass, remove them: ${stale.join(', ')}`);
});

test('a fixture skin block with --rb-text equal to --rb-bg is reported', () => {
	const css = `${THEME_CSS}\nhtml[data-skin='fixture'] .perf-root {\n\t--rb-text: #222222;\n\t--rb-bg: #222222;\n}\n`;
	const tokens = sk.skinTokens(css, 'fixture');
	assert.equal(tokens['rb-text'], tokens['rb-bg']);
	const hit = cc
		.validateScheme(tokens)
		.find((v) => v.pairing.fg === 'rb-text' && v.pairing.bg === 'rb-bg');
	assert.ok(hit, 'rb-text on rb-bg not reported');
	assert.equal(hit.ratio, 1);
});

test('the light skin merges over the light base scheme', () => {
	assert.equal(sk.skinBaseScheme(THEME_CSS, 'light'), 'light');
	assert.equal(sk.skinBaseScheme(THEME_CSS, 'green-room'), 'dark');
});
