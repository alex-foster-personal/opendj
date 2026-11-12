/**
 * Allen & Heath XONE:K2 device map (build unit: allen-and-heath-xone-k2 map).
 *
 * SOURCES (every wire number below is cited, none invented):
 *   [AP8509] XONE:K2 User Guide, Allen & Heath Publication AP8509.
 *     Canonical: https://www.allen-heath.com/media/Xone+K2_UG_AP8509_2.pdf
 *     Copy read: https://imagescdn.juno.co.uk/manual/437059-01U.pdf
 *     sha256 9a10a3267a29ea32c28e24e6797c052025c0b867deadf2fc1e7b3f26a6817df8
 *     (fetched Tue 10 Nov 2026). Sections: p.6 port name `XONE:K2`; p.9 relative
 *     encoders; p.11 default MIDI channel 15; pp.12-13 layers off = red wires;
 *     pp.14-16 MIDI Implementation Send (red rows only); pp.17-18 Return has notes
 *     but no velocity.
 *   Clean room: no Mixxx/GPL mapping and no
 *   tools/deck-diagrams/devices/allen-and-heath-xone-k2/midi.json.
 *   NOT HARDWARE CONFIRMED. No unit was available; the WebMIDI port name follows
 *   guide p.6.
 *
 * Layout: deck 1 = columns 1-2, deck 2 = columns 3-4 (outer column = deck section,
 * inner column = mixer strip). The unit has no physical crossfader: column 1 pot
 * row 3 (CC 12) is bound as a rotary crossfader; CC 15 is master. No jog wheel:
 * outer top encoders (CC 0 / CC 3) bind deck_jog with relative encoding (p.9).
 * Color FX is the outer pot row 2 filter (CC 8 / CC 11). No beat FX MidiAction
 * exists; instant_fx pad mode is inert, so nothing binds beat FX. No LED rules
 * (Return chart gives no velocity). No shift layer (SHIFT is a hint only).
 *
 * Requirements (mini-PRD):
 *   ✔︎ Two decks: play, cue, sync, 4 hot cues, load, PFL, trim, 3-band EQ, color
 *     FX filter, channel faders, 7-bit pitch on outer faders, jog on outer encoders,
 *     rotary crossfader, master, SCROLL browse encoder - all cited to [AP8509].
 *     [if] a layers-off Send wire is neither bound nor hinted
 *       [then ⛔️] map is incomplete
 *     [if] a binding carries (ch, kind, id) absent from [AP8509] layers-off Send
 *       [then ⛔️] fabricated mapping, reject in review
 */

import type { ControlHint, DeviceMap, DeviceMapEntry, MidiBinding } from '$lib/rb/midi/midi-types';

// [AP8509 p.11] default MIDI channel
const CH = 15;

/** Per-deck rows. `outer` / `inner` are 0-based column indices (deck 1: 0,1;
 * deck 2: 3,2). CC = row base + column index; note values are literals. */
function _deckBindings(deck: 1 | 2, outer: 0 | 3, inner: 1 | 2): MidiBinding[] {
	return [
		// [AP8509 p.14] top encoder turn -> deck_jog (relative, p.9)
		{
			source: { ch: CH, kind: 'cc', id: outer },
			action: { type: 'deck_jog', deck },
			relative: true
		},
		// [AP8509 p.14] pot row 1 -> trim
		{
			source: { ch: CH, kind: 'cc', id: 4 + outer },
			action: { type: 'mixer_channel', deck, target: 'trim' }
		},
		// [AP8509 p.14] pot row 2 outer -> color FX filter
		{
			source: { ch: CH, kind: 'cc', id: 8 + outer },
			action: { type: 'mixer_channel', deck, target: 'filter' }
		},
		// [AP8509 p.15] outer linear fader -> pitch (7-bit, invert: down = faster)
		{
			source: { ch: CH, kind: 'cc', id: 16 + outer },
			action: { type: 'deck_pitch', deck, lsbOffset: null },
			invert: true
		},
		// [AP8509 p.14] pot row 1 inner -> EQ high
		{
			source: { ch: CH, kind: 'cc', id: 4 + inner },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		// [AP8509 p.14] pot row 2 inner -> EQ mid
		{
			source: { ch: CH, kind: 'cc', id: 8 + inner },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		// [AP8509 p.15] pot row 3 inner -> EQ low
		{
			source: { ch: CH, kind: 'cc', id: 12 + inner },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [AP8509 p.15] inner linear fader -> channel fader
		{
			source: { ch: CH, kind: 'cc', id: 16 + inner },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		// [AP8509 p.15] switch row 3 inner -> PFL (F2=41 deck 1 / F#2=42 deck 2)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 41 : 42 },
			action: { type: 'channel_cue', deck }
		},
		// [AP8509 p.15] matrix A-D left -> hot cue A (C2=36 / D2=38)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 36 : 38 },
			action: { type: 'deck_hot_cue', deck, slot: 'A' }
		},
		// [AP8509 p.15] matrix A-D right -> hot cue B (C#2=37 / Eb2=39)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 37 : 39 },
			action: { type: 'deck_hot_cue', deck, slot: 'B' }
		},
		// [AP8509 p.15] matrix E-H left -> hot cue C (G#1=32 / Bb1=34)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 32 : 34 },
			action: { type: 'deck_hot_cue', deck, slot: 'C' }
		},
		// [AP8509 p.15] matrix E-H right -> hot cue D (A1=33 / B1=35)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 33 : 35 },
			action: { type: 'deck_hot_cue', deck, slot: 'D' }
		},
		// [AP8509 p.16] matrix I-L left -> browse load (E1=28 / F#1=30)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 28 : 30 },
			action: { type: 'browse_load', deck }
		},
		// [AP8509 p.16] matrix I-L right -> sync (F1=29 / G1=31)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 29 : 31 },
			action: { type: 'deck_sync_toggle', deck }
		},
		// [AP8509 p.16] matrix M-P left -> cue (C1=24 / D1=26)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 24 : 26 },
			action: { type: 'deck_cue', deck }
		},
		// [AP8509 p.16] matrix M-P right -> play (C#1=25 / Eb1=27)
		{
			source: { ch: CH, kind: 'note', id: deck === 1 ? 25 : 27 },
			action: { type: 'deck_play_toggle', deck }
		}
	];
}

/** Global (deck-independent) rows on [AP8509] channel 15. */
const _GLOBAL_BINDINGS: MidiBinding[] = [
	// [AP8509 p.15] column 1 pot row 3 -> rotary crossfader (unit has no crossfader)
	{
		source: { ch: CH, kind: 'cc', id: 12 },
		action: { type: 'mixer_global', target: 'crossfader' }
	},
	// [AP8509 p.15] column 4 pot row 3 -> master
	{ source: { ch: CH, kind: 'cc', id: 15 }, action: { type: 'mixer_global', target: 'master' } },
	// [AP8509 p.16] bottom-right encoder turn (SCROLL) -> browse
	{ source: { ch: CH, kind: 'cc', id: 21 }, action: { type: 'browse_encoder' }, relative: true }
];

/** Documented-but-unbound controls ([AP8509] layers-off Send, pp.14-16). */
const _HINTS: ControlHint[] = [
	// [AP8509 p.14] encoder 2 and 3 turn (columns 2 and 3)
	{ source: { ch: CH, kind: 'cc', id: 1 }, label: 'Encoder 2 turn (column 2)' },
	{ source: { ch: CH, kind: 'cc', id: 2 }, label: 'Encoder 3 turn (column 3)' },
	// [AP8509 p.14] encoder pushes E3..G3 = 52..55
	{ source: { ch: CH, kind: 'note', id: 52 }, label: 'Encoder 1 push' },
	{ source: { ch: CH, kind: 'note', id: 53 }, label: 'Encoder 2 push' },
	{ source: { ch: CH, kind: 'note', id: 54 }, label: 'Encoder 3 push' },
	{ source: { ch: CH, kind: 'note', id: 55 }, label: 'Encoder 4 push' },
	// [AP8509 p.14] switch row 1 under pot row 1
	{ source: { ch: CH, kind: 'note', id: 48 }, label: 'Switch under pot row 1, column 1' },
	{ source: { ch: CH, kind: 'note', id: 49 }, label: 'Switch under pot row 1, column 2' },
	{ source: { ch: CH, kind: 'note', id: 50 }, label: 'Switch under pot row 1, column 3' },
	{ source: { ch: CH, kind: 'note', id: 51 }, label: 'Switch under pot row 1, column 4' },
	// [AP8509 p.14] switch row 2 under pot row 2
	{ source: { ch: CH, kind: 'note', id: 44 }, label: 'Switch under pot row 2, column 1' },
	{ source: { ch: CH, kind: 'note', id: 45 }, label: 'Switch under pot row 2, column 2' },
	{ source: { ch: CH, kind: 'note', id: 46 }, label: 'Switch under pot row 2, column 3' },
	{ source: { ch: CH, kind: 'note', id: 47 }, label: 'Switch under pot row 2, column 4' },
	// [AP8509 p.15] switch row 3 columns 1 and 4 only (2 and 3 are PFL bindings)
	{ source: { ch: CH, kind: 'note', id: 40 }, label: 'Switch under pot row 3, column 1' },
	{ source: { ch: CH, kind: 'note', id: 43 }, label: 'Switch under pot row 3, column 4' },
	// [AP8509 p.16] bottom row
	{ source: { ch: CH, kind: 'note', id: 12 }, label: 'LAYER' },
	{ source: { ch: CH, kind: 'note', id: 13 }, label: 'Bottom-left encoder push' },
	{ source: { ch: CH, kind: 'cc', id: 20 }, label: 'Bottom-left encoder turn' },
	{ source: { ch: CH, kind: 'note', id: 14 }, label: 'SCROLL encoder push (SEL)' },
	{ source: { ch: CH, kind: 'note', id: 15 }, label: 'SHIFT' }
];

/** Allen & Heath XONE:K2 live map (layers-off / red wires only). */
export const XONE_K2_MAP: DeviceMap = {
	vendor: 'Allen & Heath',
	nameMatch: '\\bXONE[:_ ]?K2\\b',
	bindings: [..._deckBindings(1, 0, 1), ..._deckBindings(2, 3, 2), ..._GLOBAL_BINDINGS],
	hints: _HINTS
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'XONE_K2_MAP', map: XONE_K2_MAP, live: true }
];
