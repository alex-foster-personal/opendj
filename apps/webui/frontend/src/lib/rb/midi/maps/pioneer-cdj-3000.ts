/**
 * Pioneer DJ CDJ-3000 device map (practice replica, epic #9366).
 *
 * SOURCE OF TRUTH for every number: the official "CDJ-3000 MIDI Message List"
 * (E108, 5 pages, Feb 2021), not redistributed; URL and sha256 in
 * docs/controller/research/CDJ-3000-MIDI-Message-List.md. Part numbers in
 * [PDF] comments are that document's figure numbers (1 to 40).
 *
 * The player sends every message on ONE channel, n = its player number
 * (1 to 4). This map binds the four channels as decks 1 to 4, so a pair of
 * players linked as player 1 and 2 drives deck 1 and deck 2. The practice
 * panel (tools/deck-diagrams/devices/cdj-3000/panel.json) is one player and
 * carries the channel-1 wires.
 *
 * Bound ([PDF] number: wire -> action):
 *   7 PLAY/PAUSE note 0, 8 CUE note 1, 3 TEMPO SLIDER CC 29 (7-bit, 0 at the
 *   minus end, no LSB), 26 SYNC note 22, 32 LOOP IN note 35 and LOOP OUT
 *   note 36 (manual loop cycle), 33 RELOOP note 37 (loop exit), 34 BEAT LOOP
 *   notes 38 to 47 (1/16 to 32 beats; the 4 and 8 BEAT LOOP buttons are the
 *   notes 44 and 45 rows, the rest are touch-screen buttons), 31 HOT CUE A to
 *   H notes 27 to 34, 37 KEY SYNC note 72, 38/39 KEY SHIFT +1/-1 notes 73/74,
 *   6 ENCODER CC 79 (relative, two's complement), 21 ENCODER PUSH note 17
 *   (loads the selected track, like LOAD on a controller), 1 JOG (TOUCH)
 *   CC 16 and 5 JOG RING CC 48 (speed centred on 64, hence relativeCentered).
 *
 * Hints (no MidiAction yet, placed inert on the replica): search and track
 * search, QUANTIZE, SLIP, TIME/A.CUE, TEMPO RESET, TEMPO RANGE, MT, JOG MODE,
 * MEMORY, CALL, DELETE, BACK, TAG TRACK, TRACK FILTER, MASTER, JOG TOUCH,
 * SLIP REVERSE, REVERSE, HOT CUE CALL/DELETE, the odd beat-loop lengths, BEAT
 * JUMP, KEY RESET, Needle Position and the TOUCH/BRAKE knob.
 *
 * The list documents MIDI-IN only (no MIDI-OUT rows), so there are no LED
 * rules: the player lights its own buttons.
 *
 * NOT HARDWARE CONFIRMED. A CDJ-3000X port also matches this nameMatch; its
 * own list was not checked.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import { pioneerCc, pioneerDeckChannel, pioneerNote } from './pioneer-deck-bindings';

const DECKS = [1, 2, 3, 4] as const;
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
/** [PDF] 34 BEAT LOOP 1/16 .. 32: notes 38 .. 47 in this order. */
const BEAT_LOOP_LADDER: readonly number[] = [0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8, 16, 32];

function _deckBindings(deck: DeckId): MidiBinding[] {
	const ch = pioneerDeckChannel(deck);
	return [
		{ source: pioneerNote(ch, 0), action: { type: 'deck_play_toggle', deck } },
		{ source: pioneerNote(ch, 1), action: { type: 'deck_cue', deck } },
		{ source: pioneerCc(ch, 29), action: { type: 'deck_pitch', deck, lsbOffset: null } },
		{ source: pioneerNote(ch, 22), action: { type: 'deck_sync_toggle', deck } },
		{ source: pioneerNote(ch, 35), action: { type: 'deck_manual_loop_cycle', deck } },
		{ source: pioneerNote(ch, 36), action: { type: 'deck_manual_loop_cycle', deck } },
		{ source: pioneerNote(ch, 37), action: { type: 'deck_loop_exit', deck } },
		{ source: pioneerNote(ch, 72), action: { type: 'deck_key_sync_toggle', deck } },
		{ source: pioneerNote(ch, 73), action: { type: 'deck_key_nudge', deck, semitones: 1 } },
		{ source: pioneerNote(ch, 74), action: { type: 'deck_key_nudge', deck, semitones: -1 } },
		{ source: pioneerNote(ch, 17), action: { type: 'browse_load', deck } },
		{ source: pioneerCc(ch, 79), action: { type: 'browse_encoder' }, relative: true },
		{
			source: pioneerCc(ch, 16),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		},
		{
			source: pioneerCc(ch, 48),
			action: { type: 'deck_jog', deck },
			relative: true,
			relativeCentered: true
		},
		...HOT_CUE_SLOTS.map((slot, pad): MidiBinding => ({
			source: pioneerNote(ch, 27 + pad),
			action: { type: 'deck_hot_cue', deck, slot }
		})),
		...BEAT_LOOP_LADDER.map((beats, i): MidiBinding => ({
			source: pioneerNote(ch, 38 + i),
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
	const oddLoops: [number, string][] = [
		[48, '1/3'],
		[49, '3/4'],
		[50, '3'],
		[51, '6'],
		[52, '9'],
		[76, '3/2'],
		[77, '5'],
		[78, '7']
	];
	const jumpSizes: [number, string][] = [
		[56, '1/2'],
		[58, '1'],
		[60, '2'],
		[62, '4'],
		[64, '8'],
		[66, '16'],
		[68, '32'],
		[70, '64']
	];
	return [
		// [PDF] 2 and 4: touch-screen needle and the VINYL SPEED ADJ. TOUCH/BRAKE knob.
		cc(28, 'Needle Position (touch screen)'),
		cc(30, 'VINYL SPEED ADJ. TOUCH/BRAKE'),
		// [PDF] 9 to 20, 22 to 25, 27 to 30: buttons with no MidiAction yet.
		note(2, 'SEARCH FWD'),
		note(3, 'SEARCH REV'),
		note(4, 'TRACK SEARCH NEXT'),
		note(5, 'TRACK SEARCH PREV'),
		note(6, 'QUANTIZE'),
		note(7, 'SLIP'),
		note(8, 'TIME MODE/AUTO CUE'),
		note(9, 'TEMPO RESET'),
		note(10, 'TEMPO RANGE'),
		note(11, 'MASTER TEMPO'),
		note(12, 'JOG MODE'),
		note(13, 'MEMORY'),
		note(14, 'CUE/LOOP CALL next'),
		note(15, 'CUE/LOOP CALL previous'),
		note(16, 'DELETE'),
		note(18, 'BACK'),
		note(19, 'TAG TRACK/REMOVE'),
		note(20, 'TRACK FILTER/EDIT'),
		note(21, 'MASTER'),
		note(23, 'JOG TOUCH'),
		note(24, 'DIRECTION SLIP REV'),
		note(25, 'DIRECTION REV'),
		note(26, 'HOT CUE CALL/DELETE'),
		// [PDF] 34: beat-loop lengths outside the power-of-two ladder (touch screen).
		...oddLoops.map(([id, beats]) => note(id, `BEAT LOOP ${beats} (touch screen)`)),
		// [PDF] 35 and 36: BEAT JUMP buttons (16 beats) and the touch-screen sizes.
		note(54, '16 BEAT JUMP FWD'),
		note(55, '16 BEAT JUMP REV'),
		...jumpSizes.flatMap(([id, beats]) => [
			note(id, `BEAT JUMP FWD ${beats} (touch screen)`),
			note(id + 1, `BEAT JUMP REV ${beats} (touch screen)`)
		]),
		// [PDF] 40
		note(75, 'KEY RESET (touch screen)')
	];
}

export const CDJ3000_MAP: DeviceMap = {
	vendor: 'Pioneer DJ',
	nameMatch: 'CDJ-3000',
	bindings: DECKS.flatMap((deck) => _deckBindings(deck)),
	leds: [],
	hints: DECKS.flatMap((deck) => _deckHints(deck))
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'CDJ3000_MAP', map: CDJ3000_MAP, live: true }
];
