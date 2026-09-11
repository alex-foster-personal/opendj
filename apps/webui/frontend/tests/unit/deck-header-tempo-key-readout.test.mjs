/**
 * Pin 815937c87bc1 (issue #931): deck tempo readout gains a small
 * Camelot-coloured key line above the main tempo number (current key, with
 * the original in parentheses), and the tempo line itself gains a percentage
 * offset with the original bpm in parentheses. Hovering either "(from X)"
 * shows an explainer with a working "Reset to original" button.
 *
 * Convention (stated as a scope assumption in the PR): the annotated "(from
 * X)" form only appears once there IS a difference from the original -
 * mirrors the existing key-badge hover ("was X, shift N" only when
 * key_shift_semitones !== 0). At zero offset the readout shows the plain
 * value, same as today.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const header = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/rb/deck/DeckHeader.svelte', import.meta.url)),
	'utf8'
);

function readoutBlock() {
	const anchor = header.indexOf('class="readout"');
	assert.notEqual(anchor, -1, 'no readout block in DeckHeader');
	// The readout is wrapped in a ControlExplainer (hover reset chrome);
	// take the nearest preceding open tag through the matching close.
	const open = header.lastIndexOf('<ControlExplainer', anchor);
	assert.notEqual(open, -1, 'readout is not wrapped in a ControlExplainer');
	const close = header.indexOf('</ControlExplainer>', anchor);
	assert.notEqual(close, -1, 'readout ControlExplainer never closed');
	return header.slice(open, close + '</ControlExplainer>'.length);
}

test('readout carries a key line above the main tempo number, both Camelot-coloured, only when shifted', () => {
	const block = readoutBlock();
	assert.match(block, /keyChanged/, 'no keyChanged gate for the annotated key line');
	assert.match(block, /\(from /, 'no "(from X)" annotation for the key line');
	assert.match(block, /keyColor/, 'current key segment is not Camelot-coloured');
	assert.match(block, /origKeyColor/, 'original key segment is not Camelot-coloured');
	assert.match(header, /origKeyColor[^\n]*=[^\n]*camelotKeyColor\(deck\.key\)/, 'origKeyColor is not derived from deck.key\'s own Camelot colour');
});

test('readout tempo line carries a percentage-offset-from-original annotation, only when pitched', () => {
	const block = readoutBlock();
	assert.match(block, /tempoChanged/, 'no tempoChanged gate for the percent-offset line');
	assert.match(block, /%/, 'no percent sign in the tempo readout');
});

test('hovering the readout wraps it in ControlExplainer with a working reset action', () => {
	const block = readoutBlock();
	assert.match(block, /<ControlExplainer\b/, 'readout has no hover explainer');
	assert.match(header, /Reset to original tempo/i);
});

test('reset action drives tempo back to 0% AND composes the existing single-semitone key nudge back to 0 (no new absolute key-reset command)', () => {
	assert.match(header, /async function resetToOriginal/);
	assert.match(header, /onResetTempo\(1\)/, 'reset must set pitch ratio back to 1.0 (0%)');
	assert.match(header, /onKeyNudge\(/, 'key reset must reuse the existing relative nudge primitive, not a new absolute command');
});

test('DeckHeader mounts exactly 4 ControlExplainers: KEY SYNC, BEAT SYNC, MASTER, and the new tempo/key reset', () => {
	const mounts = header.match(/<ControlExplainer\b/g) ?? [];
	assert.equal(mounts.length, 4);
});
