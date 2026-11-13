// requirement: KEYB-14089
/**
 * #14068: spoken names for keyboard selector ids (selector-spoken-name.ts), alone and through
 * the selector announcement.
 *
 * Regression lines:
 * - if a registered selector id is spoken with a dot, a dash or a bare id then broken
 * - if an id segment missing from the table is spoken with dots or dashes then broken
 * - if an explicit node label loses to the derived name then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const spoken = await loadTypeScriptModule('src/lib/keyboard/selector-spoken-name.ts');
const announce = await loadTypeScriptModule('src/lib/keyboard/selector-announce.ts');

const SPOKEN_BY_ID = {
	'deck.1.cues': 'Deck 1, Hot cues',
	'deck.3.pitch': 'Deck 3, Tempo',
	'deck.2.cues.pad8': 'Deck 2, Hot cues, Pad 8',
	'deck.1.cues.mode': 'Deck 1, Hot cues, Pad mode',
	'deck.1.cues.grid-adjust': 'Deck 1, Hot cues, Grid adjust',
	'deck.4.loop.jump-size-down': 'Deck 4, Loop, Beat jump size down',
	mixer: 'Mixer',
	'mixer.ch2': 'Channel 2',
	'mixer.ch2.fader': 'Channel 2, Channel fader',
	'mixer.ch1.stem.vocal': 'Channel 1, Vocal stem',
	'mixer.ch2.cal.ceiling': 'Channel 2, Calibration, Ceiling',
	'mixer.ch3.fx': 'Channel 3, Color FX',
	'mixer.assign.a.ch4': 'Crossfader assign A, Channel 4',
	'mixer.crossfade-curve': 'Crossfader curve',
	'sampler.l': 'Left sampler',
	'sampler.r.pad3': 'Right sampler, Pad 3',
	topbar: 'Top bar',
	'topbar.mode': 'App mode',
	'topbar.beat-sync-max': 'Beat sync max',
	'waveforms.deck2': 'Waveforms, Deck 2',
	'library.tracks': 'Library, Track list'
};

// [if] the selection moves to deck.1.cues or another registered id [then] it says a readable name with no dots, [else stop].
test('registered selector ids are spoken as comma separated words', () => {
	for (const [id, name] of Object.entries(SPOKEN_BY_ID)) {
		assert.equal(spoken.selectorSpokenName(id), name, id);
		assert.doesNotMatch(name, /[.-]/, id);
	}
	assert.equal(
		announce.selectorAnnouncement('deck.1.loop', 'deck.1.cues', null),
		'Deck 1, Hot cues'
	);
});

// [if] an id has a segment missing from the table [then] the fallback still yields words without dots or dashes, [else stop].
test('unknown segments fall back to capitalised words', () => {
	assert.equal(spoken.selectorSpokenName('deck.2.mystery-knob'), 'Deck 2, Mystery knob');
	assert.equal(spoken.selectorSpokenName('topbar.track-info'), 'Track info');
	assert.equal(spoken.selectorSpokenName('brand_new.thing3--x'), 'Brand new, Thing 3 x');
	assert.doesNotMatch(spoken.selectorSpokenName('a-b.c-d.e_f'), /[.\-_]/);
});

// [if] a node registers an explicit label [then] that label wins over the derived name, [else stop].
test('an explicit label wins over the derived name', () => {
	assert.equal(spoken.selectorSpokenName('deck.1.cues', 'Performance pads'), 'Performance pads');
	assert.equal(spoken.selectorSpokenName('deck.1.cues', '  '), 'Deck 1, Hot cues');
	assert.equal(spoken.selectorSpokenName('deck.1.cues', null), 'Deck 1, Hot cues');
	assert.equal(announce.selectorAnnouncement(null, 'mixer.ch1', 'Left channel'), 'Left channel');
});
