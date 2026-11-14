// requirement: KEYB-14546
/**
 * #14540: spoken names for the deck transport, jog and stem leaves and the mixer headphone
 * leaves (selector-spoken-name.ts). The ids are listed literally, so the test needs none of
 * the branches that register them (#14374, #14402, #14494).
 *
 * Regression lines:
 * - if deck.1.transport.cue is spoken as the headphone cue then broken
 * - if a transport, jog, headphone or stem leaf is spoken with a raw id segment then broken
 * - if mixer.ch1.cue stops being spoken as the headphone cue then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const spoken = await loadTypeScriptModule('src/lib/keyboard/selector-spoken-name.ts');

const LEAF_NAMES = {
	'deck.1.transport.cue': 'Deck 1, Transport, Cue',
	'deck.1.transport.play': 'Deck 1, Transport, Play',
	'deck.1.transport.call-prev': 'Deck 1, Transport, Call previous cue',
	'deck.1.transport.call-next': 'Deck 1, Transport, Call next cue',
	'deck.2.jog.dial': 'Deck 2, Jog wheel, Jog dial',
	'deck.2.jog.quantize': 'Deck 2, Jog wheel, Quantize',
	'deck.2.jog.slip': 'Deck 2, Jog wheel, Slip',
	'deck.2.jog.master-tempo': 'Deck 2, Jog wheel, Master tempo',
	'deck.2.jog.auto-cue': 'Deck 2, Jog wheel, Auto cue',
	'deck.2.jog.manual-tempo': 'Deck 2, Jog wheel, Manual tempo',
	'mixer.headphones.mix': 'Headphones, Headphone mix',
	'mixer.headphones.level': 'Headphones, Headphone level',
	'mixer.headphones.io': 'Headphones, Show audio input and output',
	'mixer.headphones.midi': 'Headphones, Open MIDI panel',
	'deck.3.stems.vocal': 'Deck 3, Stems, Vocal stem',
	'deck.3.stems.instrumental': 'Deck 3, Stems, Instrumental stem',
	'deck.3.stems.drums': 'Deck 3, Stems, Drums stem',
	'deck.3.stems.bass': 'Deck 3, Stems, Bass stem',
	'deck.3.stems.other': 'Deck 3, Stems, Other stem',
	'deck.3.stems.quick.aca': 'Deck 3, Stems, Acapella quick mode',
	'deck.3.stems.quick.inst': 'Deck 3, Stems, Instrumental quick mode',
	'deck.3.stems.quick.all': 'Deck 3, Stems, All stems quick mode',
	'deck.3.stems.reset': 'Deck 3, Stems, Reset on track load',
	'deck.3.stems.separate': 'Deck 3, Stems, Separate stems',
	'deck.3.stems.load': 'Deck 3, Stems, Load stems'
};

// [if] deck.1.transport.cue is named [then] the name contains Cue and not Headphone, [else stop].
test('the transport cue leaf is spoken as Cue, not the headphone cue', () => {
	const name = spoken.selectorSpokenName('deck.1.transport.cue');
	assert.match(name, /\bCue\b/);
	assert.doesNotMatch(name, /Headphone/);
});

// [if] each new transport, jog, headphone or stem leaf is named [then] it has its table name with no raw id segment, [else stop].
test('every new leaf is spoken by its table name with no raw id segment', () => {
	for (const [id, expected] of Object.entries(LEAF_NAMES)) {
		const name = spoken.selectorSpokenName(id);
		assert.equal(name, expected, id);
		for (const segment of id.split('.').filter((part) => part.includes('-'))) {
			assert.ok(!name.toLowerCase().includes(segment), `${id} speaks raw segment ${segment}`);
		}
		assert.doesNotMatch(name, /[.-]|\b(Aca|Inst|Io|Midi|Prev|Dial)\b/, id);
	}
});

// [if] mixer.ch1.cue is named [then] it is still spoken as the channel's headphone cue, [else stop].
test('the channel cue is still spoken as the headphone cue', () => {
	assert.equal(spoken.selectorSpokenName('mixer.ch1.cue'), 'Channel 1, Headphone cue');
});
