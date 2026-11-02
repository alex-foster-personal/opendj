// requirement: DECKGAP-7691e
// Ported with Preview eb84e3312d: the deck strip's playhead is a red 1px line
// and the part of the track already played sits under a dark veil.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const src = readFileSync(
	new URL('../../src/lib/components/rb/deck/StripWaveform.svelte', import.meta.url),
	'utf8'
);

test('the played veil spans 0 to the playhead and the playhead sits on top of it', () => {
	assert.match(src, /<span class="played" style=\{`width:\$\{posPct\}%`\} aria-hidden="true"><\/span>\s*<span class="pos" style=\{`left:\$\{posPct\}%`\}>/);
	const played = src.slice(src.indexOf('\t.played {'), src.indexOf('}', src.indexOf('\t.played {')));
	const pos = src.slice(src.indexOf('\t.pos {'), src.indexOf('}', src.indexOf('\t.pos {')));
	assert.match(played, /z-index: 2;/);
	assert.match(played, /pointer-events: none;/);
	assert.match(pos, /z-index: 3;/);
	assert.match(pos, /width: 1px;/);
});

test('the playhead is red on the default skin', () => {
	const pos = src.slice(src.indexOf('\t.pos {'), src.indexOf('}', src.indexOf('\t.pos {')));
	assert.match(pos, /background: var\(--rb-strip-pos, #ff2d2d\);/);
	// Control: the old white default is gone, so the default skin cannot stay white.
	assert.doesNotMatch(pos, /#ffffff/);
});
