/**
 * Pioneer DJ CDJ-850 device map (issue #10806).
 *
 * SOURCE OF TRUTH: AlphaTheta / Pioneer DJ CDJ-850 Operating Instructions
 * (DRB1646A, page feet DRB1528-B), section "List of MIDI Messages", printed
 * p26 to p27. URL: https://downloads.support.alphatheta.com/manuals/dj-players/CDJ-850/CDJ-850_DRB1646A_manual.pdf
 * sha256 926a684d440dbc803aa0d088164848b121ac41ffdbe4178190b6a0712c31438a.
 *
 * The player sends on the [MIDI CH.] set in UTILITY (1 to 16, printed p23);
 * set it to the player's number. This map binds channels 1 to 4 as decks 1 to
 * 4; a lone player on the default channel 1 is deck 1. Channels 5 to 16 are
 * unbound.
 *
 * Bound (chart wire -> action):
 *   PLAY/PAUSE note 0x00, CUE note 0x01, TEMPO CC 0x1D (deck_pitch, lsbOffset
 *   null), LOOP IN note 0x06 and LOOP OUT note 0x07 (manual loop cycle),
 *   RELOOP/EXIT note 0x08 (loop exit), AUTO BEAT LOOP notes 0x28 to 0x2B (1/2/4/8
 *   beats), Jog dial scratch CC 0x10 and outer ring CC 0x30 (deck_jog,
 *   relative centered), SELECT PUSH rotary CC 0x4F (browse_encoder, relative
 *   two's complement), SELECT PUSH press note 0x33 (browse_load).
 *
 * SELECT PUSH press (0x33): the player has no LOAD button; pressing SELECT PUSH
 * on a highlighted track loads it onto that deck (same reading as CDJ-3000).
 *
 * Hints (no MidiAction yet): SEARCH, TRACK SEARCH, MEMORY, CALL, DELETE,
 * TIME MODE, TEMPO range, MASTER TEMPO, VINYL, jog touch, DIRECTION REV, BEAT
 * SELECT, DISC EJECT, TAG TRACK, BACK, BROWSE, TAG LIST, INFO, MENU, TOUCH/
 * RELEASE (CC 0x1E).
 *
 * No SYNC, channel fader, crossfader or FX control exists on this player (the
 * chart has no such row), so none is bound; FX on a CDJ setup live on the mixer.
 *
 * The chart documents MIDI-IN only (no MIDI-OUT rows), so there are no LED rules.
 *
 * NOT HARDWARE CONFIRMED: every binding is taken from the chart, no hardware capture.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import { pioneerCc, pioneerDeckChannel, pioneerNote } from './pioneer-deck-bindings';

const DECKS = [1, 2, 3, 4] as const;
const AUTO_BEAT_LOOPS: readonly (readonly [number, number])[] = [
	[0x28, 1],
	[0x29, 2],
	[0x2a, 4],
	[0x2b, 8]
];

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{ source: pioneerNote(ch, 0x00), action: { type: 'deck_play_toggle', deck } },
		{ source: pioneerNote(ch, 0x01), action: { type: 'deck_cue', deck } },
		{ source: pioneerCc(ch, 0x1d), action: { type: 'deck_pitch', deck, lsbOffset: null } },
		{ source: pioneerNote(ch, 0x06), action: { type: 'deck_manual_loop_cycle', deck } },
		{ source: pioneerNote(ch, 0x07), action: { type: 'deck_manual_loop_cycle', deck } },
		{ source: pioneerNote(ch, 0x08), action: { type: 'deck_loop_exit', deck } },
		{ source: pioneerNote(ch, 0x33), action: { type: 'browse_load', deck } },
		{ source: pioneerCc(ch, 0x4f), action: { type: 'browse_encoder' }, relative: true },
		{
			source: pioneerCc(ch, 0x10),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		},
		{
			source: pioneerCc(ch, 0x30),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		},
		...AUTO_BEAT_LOOPS.map(([noteId, beats]): MidiBinding => ({
			source: pioneerNote(ch, noteId),
			action: { type: 'deck_beat_loop', deck, beats }
		}))
	];
}

function _deckHints(deck: DeckId): ControlHint[] {
	const ch = pioneerDeckChannel(deck);
	const note = (id: number, name: string): ControlHint => ({
		source: pioneerNote(ch, id),
		label: `${name} (deck ${deck})`
	});
	const cc = (id: number, name: string): ControlHint => ({
		source: pioneerCc(ch, id),
		label: `${name} (deck ${deck})`
	});
	return [
		cc(0x1e, 'TOUCH/RELEASE (VINYL SPEED ADJUST)'),
		note(0x02, 'SEARCH forward'),
		note(0x03, 'SEARCH reverse'),
		note(0x04, 'TRACK SEARCH next'),
		note(0x05, 'TRACK SEARCH previous'),
		note(0x0a, 'MEMORY'),
		note(0x0b, 'CUE/LOOP CALL next'),
		note(0x0c, 'CUE/LOOP CALL previous'),
		note(0x0d, 'DELETE'),
		note(0x0e, 'TIME MODE/AUTO CUE'),
		note(0x10, 'TEMPO range (6/10/16/WIDE)'),
		note(0x11, 'MASTER TEMPO'),
		note(0x12, 'VINYL (JOG MODE)'),
		note(0x20, 'Jog dial top press (touch)'),
		note(0x21, 'DIRECTION REV'),
		note(0x2d, 'BEAT SELECT'),
		note(0x2f, 'DISC EJECT'),
		note(0x30, 'TAG TRACK/REMOVE'),
		note(0x32, 'BACK'),
		note(0x36, 'BROWSE'),
		note(0x37, 'TAG LIST'),
		note(0x38, 'INFO'),
		note(0x3f, 'MENU (UTILITY)')
	];
}

export const CDJ850_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	nameMatch: 'CDJ-850',
	bindings: DECKS.flatMap((deck) => _deckBindings(deck)),
	leds: [],
	hints: DECKS.flatMap((deck) => _deckHints(deck))
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'CDJ850_MAP', map: CDJ850_MAP, live: true }
];
