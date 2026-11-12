/**
 * Hercules DJControl MIX practice-replica device map (issue #10862, ADR-0137).
 *
 * HINTS ONLY, NO BINDINGS. Hercules publishes no MIDI message list for the DJControl MIX (its user
 * manual and product page carry none; docs/controller/research/Hercules-DJControl-MIX-sources.md
 * records the search). Every wire below is transcribed from the community Mixxx mapping
 * res/controllers/Hercules DJControl MIX.midi.xml (GPL-2.0-or-later, authors DJ Phatso, Kerrick
 * Staley; sha256 in the research note), tier 2 of (private path): a
 * community map needs a hardware spot-check before anything binds. Until that capture exists the
 * practice replica renders every control inert with the PARITY-TODO title. Nothing here is invented:
 * each hint cites the Mixxx control it came from in a comment.
 *
 * NOT in DEVICE_MAP_REGISTRY: the engine export requires at least one binding per registered map, and
 * a real unit should keep landing in the learn log as unmapped until a capture promotes hints to
 * bindings. The map is registered for the practice route only (PRACTICE_DEVICE_MAPS).
 *
 * Channel layout (Mixxx status bytes, 1-based channels):
 *   global SHIFT, crossfader, master, phones     -> channel 1
 *   deck 1 / deck 2 transport, jog, pitch, strip -> channels 2 / 3
 *   deck 1 / deck 2 SHIFT layer                  -> channels 5 / 6
 *   deck 1 / deck 2 pads and performance         -> channels 7 / 8
 *
 * SHIFT is a dedicated note on channel 1 (note 3). Shifted deck controls use the channel 5 / 6
 * twins (sync_leader, start, play_stutter, scratchWheelShift, cueMaster / cueMix).
 *
 * MIDI-OUT: the Mixxx mapping documents LED feedback on several notes; output identities are not
 * verified here, so this map carries neither leds nor meters.
 */

import type { ControlHint, DeviceMap, DeviceMapEntry } from '$lib/rb/midi/midi-types';

const note = (ch: number, id: number) => ({ ch, kind: 'note' as const, id });
const cc = (ch: number, id: number) => ({ ch, kind: 'cc' as const, id });

function _deckBaseHints(deck: 1 | 2): ControlHint[] {
	const ch = deck + 1;
	const d = `(deck ${deck})`;
	return [
		// [Mixxx] play 0x07 on channel 2 / 3.
		{ source: note(ch, 7), label: `PLAY/PAUSE ${d}` },
		// [Mixxx] cue_default 0x06.
		{ source: note(ch, 6), label: `CUE ${d}` },
		// [Mixxx] sync_enabled 0x05.
		{ source: note(ch, 5), label: `SYNC ${d}` },
		// [Mixxx] pfl 0x0C.
		{ source: note(ch, 12), label: `PFL (channel ${deck})` },
		// [Mixxx] DJCMIX.wheelTouch 0x08.
		{ source: note(ch, 8), label: `JOG touch ${d}` },
		// [Mixxx] rate MSB CC 0x08, LSB CC 0x28 (40).
		{ source: cc(ch, 8), label: `PITCH MSB ${d}` },
		{ source: cc(ch, 40), label: `PITCH LSB ${d}` },
		// [Mixxx] volume CC 0x00, super1 (filter) CC 0x01, parameter1 (EQ low) CC 0x02.
		{ source: cc(ch, 0), label: `VOLUME (channel ${deck})` },
		{ source: cc(ch, 1), label: `FILTER (channel ${deck})` },
		{ source: cc(ch, 2), label: `BASS/EQ low (channel ${deck})` },
		// [Mixxx] DJCMIX.bendWheel CC 0x09, DJCMIX.scratchWheel CC 0x0A.
		{ source: cc(ch, 9), label: `JOG bend (outer) ${d}` },
		{ source: cc(ch, 10), label: `JOG scratch (top) ${d}` }
	];
}

function _deckShiftHints(deck: 1 | 2): ControlHint[] {
	const ch = deck + 4;
	const d = `(deck ${deck})`;
	return [
		// [Mixxx] sync_leader, start, play_stutter on channels 5 / 6.
		{ source: note(ch, 5), label: `SYNC + SHIFT (sync leader) ${d}` },
		{ source: note(ch, 6), label: `CUE + SHIFT (track start) ${d}` },
		{ source: note(ch, 7), label: `PLAY + SHIFT (stutter) ${d}` },
		// [Mixxx] DJCMIX.wheelTouchShift 0x08, scratchWheelShift CC 0x0A.
		{ source: note(ch, 8), label: `JOG touch + SHIFT ${d}` },
		{ source: cc(ch, 10), label: `JOG scratch + SHIFT ${d}` },
		// [Mixxx] cueMaster (ch 5) / cueMix (ch 6) note 0x0C.
		{ source: note(ch, 12), label: `PFL + SHIFT (cue master/mix) ${d}` }
	];
}

function _padHints(deck: 1 | 2): ControlHint[] {
	const ch = deck + 6;
	const d = `(deck ${deck})`;
	const hints: ControlHint[] = [];
	for (let pad = 0; pad < 4; pad++) {
		// [Mixxx] hotcue_N_activate notes 0..3, hotcue_N_clear notes 8..11 on pad channel.
		hints.push({ source: note(ch, pad), label: `PAD ${pad + 1} hot cue ${d}` });
		hints.push({ source: note(ch, pad + 8), label: `PAD ${pad + 1} + SHIFT (clear) ${d}` });
	}
	for (let i = 0; i < 4; i++) {
		// [Mixxx] cue_gotoandplay sampler pads notes 16..19.
		hints.push({ source: note(ch, 16 + i), label: `PAD sampler ${i + 1} ${d}` });
	}
	for (let i = 0; i < 3; i++) {
		// [Mixxx] enabled effect toggles notes 32..34.
		hints.push({ source: note(ch, 32 + i), label: `FX ${i + 1} enable ${d}` });
	}
	// [Mixxx] group_Channel_enable note 35, effect_selector notes 40..42, beatloop notes 48..51.
	hints.push({ source: note(ch, 35), label: `FX channel enable ${d}` });
	for (let i = 0; i < 3; i++) {
		hints.push({ source: note(ch, 40 + i), label: `FX select ${i + 1} + SHIFT ${d}` });
	}
	for (const beats of [48, 49, 50, 51]) {
		hints.push({ source: note(ch, beats), label: `Loop ${beats - 47} beats pad mode ${d}` });
	}
	return hints;
}

function _globalHints(): ControlHint[] {
	return [
		// [Mixxx] DJCMIX.shiftButton note 0x03 on channel 1.
		{ source: note(1, 3), label: 'SHIFT' },
		// [Mixxx] crossfader CC 0x00, gain CC 0x03, headGain CC 0x04 on channel 1.
		{ source: cc(1, 0), label: 'CROSSFADER' },
		{ source: cc(1, 3), label: 'MASTER' },
		{ source: cc(1, 4), label: 'HEADPHONE volume' },
		// [Mixxx] DJCMIX.vinylButton note 0x03 on channel 2 (deck 1 only on hardware).
		{ source: note(2, 3), label: 'VINYL (deck 1)' }
	];
}

export const HERCULES_DJCONTROL_MIX_MAP: DeviceMap = {
	vendor: 'Hercules',
	// Must match "DJControl MIX" and "Hercules DJControl MIX MIDI 1" but not "DJControl MIX Ultra"
	// or the Inpulse / RMX families (regex_lite-safe: no look-around).
	nameMatch: 'DJControl MIX(?:$| MIDI)',
	bindings: [],
	hints: [
		..._globalHints(),
		..._deckBaseHints(1),
		..._deckBaseHints(2),
		..._deckShiftHints(1),
		..._deckShiftHints(2),
		..._padHints(1),
		..._padHints(2)
	]
};

export const DEVICE_MAP_ENTRIES: readonly DeviceMapEntry[] = [
	{ key: 'HERCULES_DJCONTROL_MIX_MAP', map: HERCULES_DJCONTROL_MIX_MAP, live: false }
];
