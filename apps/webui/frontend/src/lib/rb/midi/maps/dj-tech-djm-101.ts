/**
 * Dj-Tech DJM-101 P0 device map (issue #10817).
 *
 * SOURCE OF TRUTH for EVERY Note/CC number in this file: the manufacturer's
 * DJ-Tech DJM-101 User Manual MIDI hex chart, ManualsLib
 * https://www.manualslib.com/manual/1060047/Dj-Tech-Djm-101.html
 * (not redistributed here). Rows are cited as [MANUAL] with the sheet:
 *   - English FUNCTIONS DJM-101 items 1-9: ManualsLib page 5
 *   - MAIN FEATURES (17 MIDI messages, 8 selectable MIDI channels):
 *     ManualsLib page 4
 *   - Graphic MIDI hex chart (Note/CC cells): ManualsLib pages 24-28
 * USB / WebMIDI product id printed with the 101-series table on page 5:
 * `DJM-101`. Hardware pending / unconfirmed: no unit was on the bench.
 *
 * A MIXER, NOT A DECK: the DJM-101 is a compact 2-channel class-compliant
 * USB MIDI mixer. FUNCTIONS lists GAIN, 3-band EQ, MASTER VOLUME, MONITOR
 * SOURCE, MONITOR LEVEL, PFL, LED meters with a Master/PFL meter-select
 * switch, channel faders and a crossfader. It has no jog, play, cue, sync,
 * pads, browse encoder or FX knobs, so this map binds none of those
 * actions (inventing a wire would fail the chart rule). Analog C.F. Curve
 * and the MIDIOUT channel knob are not among the 17 MIDI messages.
 *
 * MIDI CHANNEL: MIDIOUT knob 1-8; factory default channel 1
 * (DJM_101_MIDI_CH). A unit on another channel needs that constant
 * changed; nothing here is per-deck channel arithmetic.
 *
 * MIDI-OUT (chart pages 24-28 MIDI-OUT column): the two PFL lamps share
 * the PFL Note cells (scientific pitch A1 / A#1) and light as Note On
 * velocity 0x7F / 0x00. The stereo LED meters take CC 80-81 in Master
 * mode and CC 82-83 in PFL mode. Those meter CCs are a stereo pair the
 * hardware switch flips; they are hinted, not stuffed into per-deck
 * `meters`. Mixxx's GPL mapping was opened only after transcription, to
 * cross-check wires already taken from the chart; the chart wins.
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiBinding
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';

/** [MANUAL] page 4 MIDIOUT factory default; knob selects MIDI channels 1-8. */
export const DJM_101_MIDI_CH = 1;

const CHANNELS = [1, 2] as const;
type Channel = (typeof CHANNELS)[number];

/** [MANUAL] pages 24-28 channel-strip CCs, in sheet order CH1 then CH2. */
const TRIM_CC: Record<Channel, number> = { 1: 0x48, 2: 0x4f };
const HI_CC: Record<Channel, number> = { 1: 0x4a, 2: 0x4d };
const MID_CC: Record<Channel, number> = { 1: 0x4e, 2: 0x4b };
const LOW_CC: Record<Channel, number> = { 1: 0x4c, 2: 0x49 };
const FADER_CC: Record<Channel, number> = { 1: 0x41, 2: 0x42 };
/** [MANUAL] pages 24-28 PFL Note On: scientific pitch A1 / A#1. */
const PFL_NOTE: Record<Channel, number> = { 1: 0x21, 2: 0x22 };

function _channelBindings(n: Channel): MidiBinding[] {
	const ch = DJM_101_MIDI_CH;
	const deck: DeckId = n;
	return [
		// [MANUAL] page 5 item 1 GAIN; pages 24-28 CC 0x48 / 0x4F.
		{
			source: { ch, kind: 'cc', id: TRIM_CC[n] },
			action: { type: 'mixer_channel', deck, target: 'trim' }
		},
		// [MANUAL] page 5 item 2 HI / MID / LOW; pages 24-28 CCs above.
		{
			source: { ch, kind: 'cc', id: HI_CC[n] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'high' }
		},
		{
			source: { ch, kind: 'cc', id: MID_CC[n] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'mid' }
		},
		{
			source: { ch, kind: 'cc', id: LOW_CC[n] },
			action: { type: 'mixer_channel', deck, target: 'eq', band: 'low' }
		},
		// [MANUAL] page 5 item 8 channel volume fader; pages 24-28 CC 0x41 / 0x42.
		{
			source: { ch, kind: 'cc', id: FADER_CC[n] },
			action: { type: 'mixer_channel', deck, target: 'fader' }
		},
		// [MANUAL] page 5 item 6 illuminated PFL; pages 24-28 Note 0x21 / 0x22.
		{ source: { ch, kind: 'note', id: PFL_NOTE[n] }, action: { type: 'channel_cue', deck } }
	];
}

function _globalBindings(): MidiBinding[] {
	const ch = DJM_101_MIDI_CH;
	return [
		// [MANUAL] page 5 item 9 CROSSFADER; pages 24-28 CC 0x40.
		{
			source: { ch, kind: 'cc', id: 0x40 },
			action: { type: 'mixer_global', target: 'crossfader' }
		},
		// [MANUAL] page 5 item 3 MASTER VOLUME; pages 24-28 CC 0x43.
		{ source: { ch, kind: 'cc', id: 0x43 }, action: { type: 'mixer_global', target: 'master' } },
		// [MANUAL] page 5 item 4 MONITOR SOURCE (cue / master blend); pages 24-28 CC 0x44.
		{ source: { ch, kind: 'cc', id: 0x44 }, action: { type: 'headphone_mix' } },
		// [MANUAL] page 5 item 5 MONITOR LEVEL; pages 24-28 CC 0x45.
		{ source: { ch, kind: 'cc', id: 0x45 }, action: { type: 'headphone_level' } }
	];
}

function _hints(): ControlHint[] {
	const ch = DJM_101_MIDI_CH;
	return [
		// [MANUAL] page 5 item 7 Master / PFL meter-select switch; pages 24-28
		// Note 0x23 (PFL) and Note 0x24 (Master). No P0 action; the 17th message.
		{ source: { ch, kind: 'note', id: 0x23 }, label: 'Master / PFL meter select (PFL)' },
		{ source: { ch, kind: 'note', id: 0x24 }, label: 'Master / PFL meter select (Master)' },
		// [MANUAL] pages 24-28 MIDI-OUT stereo meters. Hardware switch flips
		// the pair between Master (CC 80-81) and PFL (CC 82-83). Hinted, not
		// `meters` (those are per-deck).
		{ source: { ch, kind: 'cc', id: 0x50 }, label: 'Master meter L (MIDI-OUT)' },
		{ source: { ch, kind: 'cc', id: 0x51 }, label: 'Master meter R (MIDI-OUT)' },
		{ source: { ch, kind: 'cc', id: 0x52 }, label: 'PFL meter L (MIDI-OUT)' },
		{ source: { ch, kind: 'cc', id: 0x53 }, label: 'PFL meter R (MIDI-OUT)' }
	];
}

function _pflLeds(): LedRule[] {
	const ch = DJM_101_MIDI_CH;
	return CHANNELS.map((n) => ({
		// [MANUAL] pages 24-28 MIDI-OUT PFL lamps: same Note as the PFL press,
		// velocity 0x7F on / 0x00 off.
		trigger: { kind: 'channel_cue_enabled', deck: n },
		out: { ch, note: PFL_NOTE[n], velocityOn: 0x7f, velocityOff: 0x00 }
	}));
}

export const DJM_101_MAP: DeviceMap = {
	vendor: 'Dj-Tech',
	// WebMIDI port names carry the product id, e.g. "DJM-101",
	// "DJ-Tech DJM-101", "DJ-Tech DJM-101 MIDI 1".
	nameMatch: 'DJM-101',
	bindings: [...CHANNELS.flatMap((n) => _channelBindings(n)), ..._globalBindings()],
	leds: _pflLeds(),
	hints: _hints()
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'DJM_101_MAP', map: DJM_101_MAP, live: true }
];
