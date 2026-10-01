// requirement: CUEOUT-12 (headphone output device acquisition)
// [if] the microphone is already granted [then] pressing I/O opens no stream, because the names are already readable
// [if] the microphone is denied [then] pressing I/O opens no stream and says why, rather than reporting a failure
// [if] the browser will not answer the permission question [then] I/O asks, which is what pressing it consented to
// [if] a channel CUE button carries no headphone icon [then] the four centre columns read as bare text again
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = new URL('../../', import.meta.url);
let headphones;

before(async () => {
	globalThis.window = { localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} } };
	headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
});

test('if the microphone is already granted then I/O must not open a stream for nothing', () => {
	assert.equal(headphones.micUnlockDecision('granted'), 'already_unlocked');
});

test('if the microphone is denied then I/O must not ask again', () => {
	assert.equal(headphones.micUnlockDecision('denied'), 'declined');
});

test('if the state is prompt or unknown then pressing I/O is the consent to ask', () => {
	for (const state of ['prompt', null, undefined, 'something-new']) {
		assert.equal(headphones.micUnlockDecision(state), 'ask', `state ${String(state)}`);
	}
});

test('if the permission state is not a string then it throws rather than guessing', () => {
	assert.throws(() => headphones.micUnlockDecision(1), TypeError);
});

test('if the declined notice reads as a failure then a deliberate choice looks like a bug', () => {
	const notice = headphones.MIC_DECLINED_NOTICE;
	assert.match(notice, /practice and SPLIT do not need it at all/);
	assert.doesNotMatch(notice, /fail|error|denied/i);
});

// Source guards: the acquire path is browser-only, so the wiring is pinned as text.
test('if the acquire path unlocks unconditionally then a granted mic is still opened every time', () => {
	const source = readFileSync(new URL('src/lib/player/headphones.ts', FRONTEND), 'utf8');
	// IOPIN-14: the decision also reads whether the listing is still withheld, so a
	// `granted` that did not actually yield names is asked rather than skipped.
	const decided = source.indexOf('labelUnlockDecision(await _microphonePermissionState(), namesWithheld)');
	const unlocked = source.indexOf('await _unlockHeadphoneOutputLabels(mediaDevices)');
	assert.ok(decided > 0, 'the acquire path must consult the permission first');
	assert.ok(unlocked > decided, 'the unlock must come after the decision, not before it');
	assert.match(source, /if \(decision === 'ask'\) \{/, 'the unlock must be gated on the decision');
});

test('if a channel CUE button loses its headphone icon then the centre columns read as bare text', () => {
	const source = readFileSync(new URL('src/lib/components/rb/mixer/ChannelStrip.svelte', FRONTEND), 'utf8');
	const icon = source.indexOf('class="cue-icon"');
	const word = source.indexOf('>CUE</button');
	assert.ok(icon > 0, 'the CUE button needs the headphone icon');
	assert.ok(word > icon, 'the icon belongs to the LEFT of the word, inside the button');
	assert.match(source, /\.cue-btn \{[^}]*display: inline-flex;/s, 'the button must lay the icon and word out in a row');
});
