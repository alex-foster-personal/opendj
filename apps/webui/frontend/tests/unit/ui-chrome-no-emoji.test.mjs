/**
 * requirement: CHROME-01
 * UI chrome uses SVG icons, not Unicode emoji or text-symbol glyphs.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));

const SCOPED_FILES = [
	'components/rb/browser/TrackTable.svelte',
	'components/rb/browser/RatingStars.svelte',
	'components/rb/mixer/HeadphoneCluster.svelte',
	'components/settings/SettingsOverlay.svelte',
	'components/rb/midi/midi-format.ts',
	'components/rb/TopBar.svelte',
	'components/rb/RefreshAnalysisButton.svelte',
	'components/rb/deck/HotCueBank.svelte'
];

const BANNED_GLYPHS = ['✓', '✗', '⚡', '★', '☆', '▲', '▼', '↻', '✅', '🤖'];

/** Unicode Extended_Pictographic covers emoji blocks used as UI glyphs. */
const EMOJI_CODEPOINT_RE = /\p{Extended_Pictographic}/u;

function stripComments(source) {
	return source
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/^\s*\/\/.*$/gm, '')
		.replace(/<!--[\s\S]*?-->/g, '');
}

function findEmojiCodepoints(source) {
	const found = new Set();
	for (const match of source.matchAll(/\p{Extended_Pictographic}/gu)) {
		found.add(match[0]);
	}
	return [...found];
}

for (const rel of SCOPED_FILES) {
	test(`CHROME-01: ${rel} has no emoji or text-symbol UI glyphs`, () => {
		const source = stripComments(readFileSync(`${ROOT}/${rel}`, 'utf8'));
		for (const glyph of BANNED_GLYPHS) {
			assert.equal(source.includes(glyph), false, `${rel} still contains banned glyph ${glyph}`);
		}
		const emoji = findEmojiCodepoints(source);
		assert.equal(
			emoji.length,
			0,
			`${rel} contains emoji codepoints: ${emoji.map((g) => `${g} (U+${g.codePointAt(0).toString(16)})`).join(', ')}`
		);
	});
}

test('CHROME-01: emoji detector catches arbitrary pictographic codepoints', () => {
	assert.equal(EMOJI_CODEPOINT_RE.test('🎵'), true);
	assert.equal(EMOJI_CODEPOINT_RE.test('plain text'), false);
});
