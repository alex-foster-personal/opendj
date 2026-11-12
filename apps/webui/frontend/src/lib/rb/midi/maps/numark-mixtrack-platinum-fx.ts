/**
 * Numark Mixtrack Platinum FX practice-replica device map (issue #10875, ADR-0137).
 *
 * HINTS ONLY, NO BINDINGS. Numark publishes no MIDI message list for this controller (its
 * User Guide v1.2 and product page carry none; docs/controller/research/
 * Numark-Mixtrack-Platinum-FX-sources.md records the search). Every wire below is therefore
 * transcribed from the community Mixxx mapping, "Numark Mixtrack Platinum FX.midi.xml" and
 * Numark-Mixtrack-Platinum-FX-scripts.js (GPL-2.0-or-later; sha256 of both files in the
 * research note), which is tier 2 of
 * (private path): a community map needs a hardware spot-check
 * before anything binds to it. Until that capture exists the practice replica renders every
 * control inert with the PARITY-TODO title, which is the house rule for a wire Open DJ has not
 * verified (no fake behavior). Nothing here is invented: each hint cites the Mixxx control it
 * came from in a comment.
 *
 * NOT in DEVICE_MAP_REGISTRY: the engine export and its tests require at least one binding per
 * registered map, and a real controller plugged in today should keep landing in the learn log as
 * unmapped rather than firing unverified wires. The map is registered for the practice route
 * only (PRACTICE_DEVICE_MAPS). Promoting it to the registry is the hardware-capture follow-up.
 *
 * Channel layout ([Mixxx] status bytes):
 *   deck 1 / deck 2 transport, jog, pitch, mixer strip -> channels 1 / 2
 *   deck 1 / deck 2 pads, pad-mode and loop buttons  -> channels 5 / 6
 *   effect unit 1 / 2 (paddle, knobs, FX SELECT row) -> channels 9 / 10
 *   MASTER, CUE GAIN, CUE MIX                         -> channel 15
 *   BROWSE, LOAD 1 / 2, crossfader                    -> channel 16
 *
 * SHIFT is encoded in hardware: a shifted control sends a different note (play 0 -> 4, cue 1 -> 5,
 * sync 2 -> 3, scratch 7 -> 8, pitch bend 11/12 -> 43/44, pads 20..27 -> 28..35, loop 64 -> 65,
 * 1/2 52 -> 54, x2 53 -> 55, BROWSE turn CC 0 -> 1, press 6 -> 7). SHIFT itself is note 32.
 *
 * MIDI-OUT ([Mixxx] script sends): the LEDs answer the same note numbers as their inputs with
 * velocity 0x7F on and 0x01 dim; the channel VU meters are CC 31 on channels 1 / 2 (0..90).
 */

import type {
	ControlHint,
	DeviceMap,
	DeviceMapEntry,
	LedRule,
	MidiMeterOutput,
	MidiSource
} from '$lib/rb/midi/midi-types';
import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';

const DECKS: readonly (1 | 2)[] = [1, 2];
const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D'];
export const MIXTRACK_PLATINUM_FX_LED_ON = 0x7f;
/** [Mixxx] components.Button.prototype.off = 0x01: inactive LEDs are dimmed, never dark. */
export const MIXTRACK_PLATINUM_FX_LED_OFF = 0x01;

const note = (ch: number, id: number): MidiSource => ({ ch, kind: 'note', id });
const cc = (ch: number, id: number): MidiSource => ({ ch, kind: 'cc', id });

/** Pads, pad-mode and loop buttons of deck d live on channel 4 + d. */
function _padChannel(deck: DeckId): number {
	return 4 + deck;
}

function _deckHints(deck: 1 | 2): ControlHint[] {
	const ch = deck;
	const pch = _padChannel(deck);
	const d = `(deck ${deck})`;
	const hints: ControlHint[] = [
		// [Mixxx] playButton 0x90+ch 0x00, shiftOffset 0x04.
		{ source: note(ch, 0), label: `PLAY/PAUSE ${d}` },
		{ source: note(ch, 4), label: `PLAY/PAUSE + SHIFT ${d}` },
		// [Mixxx] cueButton 0x01, shiftOffset 0x04.
		{ source: note(ch, 1), label: `CUE ${d}` },
		{ source: note(ch, 5), label: `CUE + SHIFT (track start) ${d}` },
		// [Mixxx] syncButton 0x02, shiftOffset 0x01.
		{ source: note(ch, 2), label: `SYNC ${d}` },
		{ source: note(ch, 3), label: `SYNC + SHIFT (sync off) ${d}` },
		// [Mixxx] wheelTouch note 0x06, wheelTurn CC 0x06.
		{ source: note(ch, 6), label: `JOG touch ${d}` },
		{ source: cc(ch, 6), label: `JOG turn ${d}` },
		// [Mixxx] scratchToggle 0x07, shiftOffset 0x01 (censor / bleep).
		{ source: note(ch, 7), label: `SCRATCH ${d}` },
		{ source: note(ch, 8), label: `SCRATCH + SHIFT (bleep) ${d}` },
		// [Mixxx] pitchBendUp 0x0B (+ SHIFT 0x2B keylock), pitchBendDown 0x0C (+ SHIFT 0x2C pitch range).
		{ source: note(ch, 11), label: `PITCH BEND + ${d}` },
		{ source: note(ch, 43), label: `PITCH BEND + with SHIFT (keylock) ${d}` },
		{ source: note(ch, 12), label: `PITCH BEND - ${d}` },
		{ source: note(ch, 44), label: `PITCH BEND - with SHIFT (pitch range) ${d}` },
		// [Mixxx] pflButton 0x1B.
		{ source: note(ch, 27), label: `CUE/PFL (channel ${deck})` },
		// [Mixxx] shiftButton 0x20.
		{ source: note(ch, 32), label: `SHIFT ${d}` },
		// [Mixxx] pitch MSB CC 0x09, LSB CC 0x29 (inverted: + is at the bottom).
		{ source: cc(ch, 9), label: `PITCH fader MSB ${d}` },
		{ source: cc(ch, 41), label: `PITCH fader LSB ${d}` },
		// [Mixxx] gain CC 0x16, treble 0x17, mid 0x18, bass 0x19, filter 0x1A, volume 0x1C.
		{ source: cc(ch, 22), label: `LEVEL (channel ${deck})` },
		{ source: cc(ch, 23), label: `TREBLE (channel ${deck})` },
		{ source: cc(ch, 24), label: `MID (channel ${deck})` },
		{ source: cc(ch, 25), label: `BASS (channel ${deck})` },
		{ source: cc(ch, 26), label: `FILTER (channel ${deck})` },
		{ source: cc(ch, 28), label: `CH FADER (channel ${deck})` },
		// [Mixxx] PadModeControls: HOTCUE 0x00, BEATJUMP 0x02 (= CUE + SHIFT), FADERCUTS 0x07,
		// SAMPLE1 0x0B, AUTOLOOP 0x0D, SAMPLE2 0x0F (= SAMPLER + SHIFT), on the pad channel.
		{ source: note(pch, 0), label: `pad mode CUE ${d}` },
		{ source: note(pch, 2), label: `pad mode CUE + SHIFT (beat jump) ${d}` },
		{ source: note(pch, 13), label: `pad mode AUTO LOOP ${d}` },
		{ source: note(pch, 7), label: `pad mode FADER CUTS ${d}` },
		{ source: note(pch, 11), label: `pad mode SAMPLER ${d}` },
		{ source: note(pch, 15), label: `pad mode SAMPLER + SHIFT (bank 2) ${d}` },
		// [Mixxx] loop 0x40 (+ SHIFT 0x41 reloop), loopHalf 0x34 (+ SHIFT 0x36 loop in),
		// loopDouble 0x35 (+ SHIFT 0x37 loop out), on the pad channel.
		{ source: note(pch, 64), label: `LOOP ${d}` },
		{ source: note(pch, 65), label: `LOOP + SHIFT (reloop) ${d}` },
		{ source: note(pch, 52), label: `1/2 ${d}` },
		{ source: note(pch, 54), label: `1/2 + SHIFT (loop in) ${d}` },
		{ source: note(pch, 53), label: `x2 ${d}` },
		{ source: note(pch, 55), label: `x2 + SHIFT (loop out) ${d}` }
	];
	// [Mixxx] pads 0x14..0x1B, shiftOffset 0x08.
	for (let pad = 0; pad < 8; pad++) {
		hints.push({ source: note(pch, 20 + pad), label: `PAD ${pad + 1} ${d}` });
		hints.push({ source: note(pch, 28 + pad), label: `PAD ${pad + 1} + SHIFT ${d}` });
	}
	return hints;
}

function _effectHints(unit: 1 | 2): ControlHint[] {
	const ch = 8 + unit;
	return [
		// [Mixxx] enableSwitch CC 0x03 (0 middle, 1 up = latch, 2 down = hold).
		{ source: cc(ch, 3), label: `FX ${unit} paddle` },
		// [Mixxx] dryWetKnob CC 0x04, effectParam CC 0x05 (both units read both knobs).
		{ source: cc(ch, 4), label: `DRY/WET (effect unit ${unit})` },
		{ source: cc(ch, 5), label: `BEATS (effect unit ${unit})` }
	];
}

/** [Mixxx] prevEffect 0x98 (unit-1)*2, nextEffect 0x99 0x03+(unit-1)*2, setBeatgrid
 * 0x98+channel 0x01+channel*3, tap 0x88 0x09: the FX SELECT rows read left to right as notes
 * 0, 1, 2 on channel 9 and 3, 4, 5 on channel 10, matching the printed HPF LPF FLANGER / ECHO
 * REVERB PHASER grid. */
function _fxSelectHints(): ControlHint[] {
	return [
		{ source: note(9, 0), label: 'FX SELECT HPF' },
		{ source: note(9, 1), label: 'FX SELECT LPF (TAP + SHIFT, set beatgrid deck 1)' },
		{ source: note(9, 2), label: 'FX SELECT FLANGER' },
		{ source: note(10, 3), label: 'FX SELECT ECHO' },
		{ source: note(10, 4), label: 'FX SELECT REVERB (TAP + SHIFT, set beatgrid deck 2)' },
		{ source: note(10, 5), label: 'FX SELECT PHASER' },
		{ source: note(9, 9), label: 'TAP (BPM tap)' },
		{ source: note(10, 9), label: 'TAP (BPM tap, deck 2 twin)' }
	];
}

function _globalHints(): ControlHint[] {
	return [
		// [Mixxx] browse knob CC 0x00 (+ SHIFT 0x01), knobButton note 0x06 (+ SHIFT 0x07), channel 16.
		{ source: cc(16, 0), label: 'BROWSE turn' },
		{ source: cc(16, 1), label: 'BROWSE turn + SHIFT (fast scroll)' },
		{ source: note(16, 6), label: 'BROWSE press' },
		{ source: note(16, 7), label: 'BROWSE press + SHIFT' },
		// [Mixxx] loadButton notes 0x02 / 0x03 on channel 16.
		{ source: note(16, 2), label: 'LOAD 1' },
		{ source: note(16, 3), label: 'LOAD 2' },
		// [Mixxx] crossfader CC 0x08 on channel 16 (and a second, inverted message on channel 2).
		{ source: cc(16, 8), label: 'CROSSFADER' },
		{ source: cc(2, 8), label: 'CROSSFADER (second message, inverted)' },
		// [Mixxx] gains: mainGain CC 0x23, cueMix CC 0x27, cueGain CC 0x2F on channel 15.
		{ source: cc(15, 35), label: 'MASTER' },
		{ source: cc(15, 39), label: 'CUE MIX' },
		{ source: cc(15, 47), label: 'CUE GAIN' }
	];
}

/** [Mixxx] LED writes: play, sync, pfl and loop answer their input notes; the four top pads
 * light per hot cue in CUE mode; the pad-mode buttons light the selected mode. */
function _ledRules(deck: 1 | 2): LedRule[] {
	const ch = deck;
	const pch = _padChannel(deck);
	const out = (c: number, n: number) => ({
		ch: c,
		note: n,
		velocityOn: MIXTRACK_PLATINUM_FX_LED_ON,
		velocityOff: MIXTRACK_PLATINUM_FX_LED_OFF
	});
	const rules: LedRule[] = [
		{ trigger: { kind: 'deck_playing', deck }, out: out(ch, 0) },
		{ trigger: { kind: 'beat_sync_enabled', deck }, out: out(ch, 2) },
		{ trigger: { kind: 'channel_cue_enabled', deck }, out: out(ch, 27) },
		{ trigger: { kind: 'loop_engaged', deck }, out: out(pch, 64) },
		{ trigger: { kind: 'pad_mode_selected', deck, mode: 'hot_cue' }, out: out(pch, 0) },
		{ trigger: { kind: 'pad_mode_selected', deck, mode: 'auto_loop' }, out: out(pch, 13) },
		{ trigger: { kind: 'pad_mode_selected', deck, mode: 'sampler' }, out: out(pch, 11) }
	];
	HOT_CUE_SLOTS.forEach((slot, i) => {
		rules.push({
			trigger: { kind: 'hot_cue_present', deck, slot, padMode: 'hot_cue' },
			padMode: 'hot_cue',
			out: out(pch, 20 + i)
		});
	});
	return rules;
}

/** [Mixxx] vuCallback: CC 0x1F on channel 1 / 2, value = level * 90. */
const METERS: MidiMeterOutput[] = DECKS.map((deck) => ({
	deck,
	out: { ch: deck, cc: 0x1f, maxValue: 90 }
}));

export const NUMARK_MIXTRACK_PLATINUM_FX_MAP: DeviceMap = {
	vendor: 'Numark',
	// The USB port enumerates as "MixTrack Platinum FX"; the pattern must miss Mixtrack Pro FX
	// and the older Mixtrack Pro family.
	nameMatch: 'Mixtrack\\s*Platinum\\s*FX',
	bindings: [],
	leds: DECKS.flatMap((deck) => _ledRules(deck)),
	meters: METERS,
	hints: [
		...DECKS.flatMap((deck) => _deckHints(deck)),
		..._effectHints(1),
		..._effectHints(2),
		..._fxSelectHints(),
		..._globalHints()
	]
};

/** Self-registration: maps/entries.ts discovers this export, so no shared list names this map. */
export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{
		key: 'NUMARK_MIXTRACK_PLATINUM_FX_MAP',
		map: NUMARK_MIXTRACK_PLATINUM_FX_MAP,
		live: false
	}
];
