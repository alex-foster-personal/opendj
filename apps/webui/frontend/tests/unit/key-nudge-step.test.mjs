// requirement: DECKGAP-7691b
/**
 * Pin 783ce03c0dbd: key nudge sizes. Semitone is the standard DJ step.
 * Camelot is one fifth (one wheel number). Scale walks the track's scale.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/player/key/camelot.ts');
});

test('semitone is 1 and tone is 2, the standard chromatic steps', () => {
	assert.equal(mod.keyNudgeSemitones('semitone', 1, null, 0), 1);
	assert.equal(mod.keyNudgeSemitones('semitone', -1, '8A', 4), -1);
	assert.equal(mod.keyNudgeSemitones('tone', 1, '8A', 0), 2);
	assert.equal(mod.keyNudgeSemitones('tone', -1, null, 0), -2);
});

test('one Camelot step is a perfect fifth and moves one wheel number', () => {
	assert.equal(mod.keyNudgeSemitones('camelot', 1, '8A', 0), 7);
	assert.equal(mod.keyNudgeSemitones('camelot', -1, '8A', 0), -7);
	assert.equal(mod.effectiveCamelotKey('8A', 7), '9A');
	assert.equal(mod.effectiveCamelotKey('8A', -7), '7A');
	assert.equal(mod.effectiveCamelotKey('8B', 7), '9B');
});

test('scale walks the track scale, including the half step, and rejects an unparsed key', () => {
	assert.equal(mod.keyNudgeSemitones('scale', 1, '8A', 0), 2);
	assert.equal(mod.keyNudgeSemitones('scale', -1, '8A', 0), -2);
	assert.equal(mod.keyNudgeSemitones('scale', 1, '8A', 2), 1);
	assert.equal(mod.keyNudgeSemitones('scale', -1, '8B', 0), -1);
	assert.equal(mod.keyNudgeSemitones('scale', 1, '8B', 0), 2);
	assert.equal(mod.keyNudgeSemitones('scale', 1, 'Am', 0), 2);
	assert.equal(mod.keyNudgeSemitones('scale', 1, null, 0), null);
	assert.equal(mod.keyNudgeSemitones('scale', 1, 'nope', 0), null);
});
