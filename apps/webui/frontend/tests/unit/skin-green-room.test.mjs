// requirement: CHROME-8729
// [if] the Green Room skin misses a token mono-dev or the base palette defines [then] a light-theme value leaks in, [else stop].
// [if] a Green Room text or indicator pair falls under AA [then] it is illegible, [else stop].
// [if] the skin's user-facing text names the app that inspired it, or a brand font file ships [then] the homage becomes a copy, [else stop].
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const THEME = readFileSync('src/lib/rb/theme.css', 'utf8');
const SETTING = readFileSync('src/lib/settings/wave-palette-setting.ts', 'utf8');

let skin;
let cc;
before(async () => {
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
	cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
});

function customProps(selector) {
	const start = THEME.indexOf(`${selector} {`);
	assert.ok(start !== -1, `no \`${selector} {}\` block in theme.css`);
	const body = THEME.slice(start, THEME.indexOf('}', start));
	return new Set([...body.matchAll(/(--[\w-]+):/g)].map((m) => m[1]));
}

test('if Green Room is not a registered, labeled, dark skin on tri-band then switching to it does nothing or repaints the waveforms', () => {
	assert.ok(skin.UI_SKIN_CHOICES.includes('green-room'));
	assert.equal(skin.UI_SKIN_LABELS['green-room'], 'Green Room');
	assert.equal(skin.parseUiSkin('green-room'), 'green-room');
	assert.equal(skin.effectiveWaveScheme('light', 'green-room'), 'dark');
	assert.equal(skin.effectiveWaveformDesign('auto', 'green-room'), 'tri-band');
	globalThis.document = { documentElement: { dataset: {} } };
	try {
		skin.applyUiSkinDom('green-room');
		assert.equal(globalThis.document.documentElement.dataset.skin, 'green-room');
	} finally {
		delete globalThis.document;
	}
	assert.match(SETTING, /\{ value: 'green-room', label: 'Green Room' \}/);
});

test('if Green Room misses a token mono-dev or the base palette defines then a light-theme value leaks in', () => {
	const green = customProps("html[data-skin='green-room'] .perf-root");
	const mono = customProps("html[data-skin='mono-dev'] .perf-root");
	const baseColors = Object.keys(cc.parseColorTokens(THEME, '.perf-root')).map((k) => `--${k}`);
	assert.ok(mono.size >= 30 && baseColors.length >= 20, 'token parse looks short');
	assert.deepEqual([...mono].filter((t) => !green.has(t)), []);
	assert.deepEqual(baseColors.filter((t) => !green.has(t)), []);
});

test('if a Green Room text or indicator pair fails contrast then readouts are illegible', () => {
	const tokens = cc.parseColorTokens(THEME, "html[data-skin='green-room'] .perf-root");
	assert.equal(tokens['rb-bg'], '#121212');
	assert.equal(tokens['rb-accent'], '#1db954');
	assert.deepEqual(cc.validateScheme(tokens), []);
	assert.ok(cc.contrastRatio(tokens['rb-on-accent'], tokens['rb-accent']) >= 4.5, 'ink on the green fill');
	assert.ok(cc.contrastRatio(tokens['rb-master-text'], tokens['rb-select']) >= 4.5, 'loaded row on a selected row');
});

test('if the top-bar skin button has no glyph for a skin, or two skins share one, then it renders empty or ambiguous', () => {
	const glyphs = skin.UI_SKIN_CHOICES.map((s) => skin.SKIN_BUTTON_ICON_SVG[s]);
	for (const [i, s] of skin.UI_SKIN_CHOICES.entries()) {
		assert.match(glyphs[i] ?? '', /<(path|circle|rect)\b/, `no drawable glyph for ${s}`);
	}
	assert.equal(new Set(glyphs).size, glyphs.length, 'two skins share a glyph');
});

test('if the skin names the inspiring app or ships a brand font then it is a copy, not an homage', () => {
	assert.doesNotMatch(skin.UI_SKIN_LABELS['green-room'], /spotify/i);
	const optionBlock = SETTING.slice(SETTING.indexOf('export const UI_SKIN_SETTING'), SETTING.indexOf('export const WAVE_SPLIT_MASTER_SETTING'));
	assert.ok(optionBlock.length > 100, 'setting block not found');
	assert.doesNotMatch(optionBlock, /spotify/i);
	const greenBlock = THEME.slice(THEME.indexOf('-------- skin: green-room'), THEME.indexOf('/* -------- panel chrome'));
	assert.ok(greenBlock.length > 500, 'green-room block not found');
	assert.doesNotMatch(greenBlock, /@font-face|circular/i);
	const fonts = readdirSync('static/fonts');
	assert.ok(fonts.length > 0, 'static/fonts listing is empty');
	assert.deepEqual(fonts.filter((f) => /circular|spotify/i.test(f)), []);
});
