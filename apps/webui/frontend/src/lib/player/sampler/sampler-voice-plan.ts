/**
 * Edge sampler: the pure plan one voice plays (spec section 4.3, 4.4). Issue #10780 (S3).
 * No Web Audio here, so node tests can pin what `SamplerVoice` hands to the source or stretcher.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 a slot becomes one plan from the launch rules (rate, key, loop window) and the clip guard
 *     [if] a 120 BPM slot plays against 126 with key auto [then] rate 1.05, master tempo on [else ⛔️]
 *     [if] the slot is one shot [then] the plan never loops, whatever its repeat [else ⛔️]
 *     [if] no master plays [then] the rate is 1 (free, native tempo) [else ⛔️]
 *   ✔︎ ✅ 🎯 the start offsets skip the guard audio so beat 1 of the region lands on the launch time
 *     [if] the plan plays once [then] the plain source gets a duration of exactly the region [else ⛔️]
 *     [if] the plan loops [then] loop points are clip seconds (guard + window) [else ⛔️]
 */

import type { StretchScheduleChange } from '$lib/rb/stretch-adapter';
import { CLIP_GUARD_MS } from './api-sampler';
import { keyPlan, loopWindow, playbackRate } from './sampler-launch';
import type { SamplerSlot } from './sampler-types';

/** Stop and start gain ramp (spec 4.4: the deck's 5 ms linear stop ramp, mimicked). */
export const VOICE_STOP_RAMP_SEC = 0.005;

export interface VoicePlan {
	buffer: AudioBuffer;
	/** seconds of guard audio before the region in the clip */
	guardSec: number;
	/** region length in clip seconds (native rate) */
	regionSec: number;
	rate: number;
	semitones: number;
	/** false = pitch follows rate (plain source, semitones implied) */
	masterTempo: boolean;
	loop: boolean;
	loopStartSec: number;
	loopEndSec: number;
}

/** What the plan needs from the playing master; null when no master plays. */
export interface MasterTempoKey {
	bpm: number;
	key: string | null;
}

/** The plan a slot plays against the master (rate, key shift, loop window, guard). */
export function buildVoicePlan(
	slot: SamplerSlot,
	buffer: AudioBuffer,
	master: MasterTempoKey | null
): VoicePlan {
	const rate = playbackRate(slot.native_bpm, master?.bpm ?? null, slot.settings.tempo);
	const key = keyPlan(slot.key, master?.key ?? null, slot.settings.key, rate);
	const regionSec = (slot.out_ms - slot.in_ms) / 1000;
	const repeat = slot.settings.mode === 'one_shot' ? 'once' : slot.settings.repeat;
	return {
		buffer,
		guardSec: CLIP_GUARD_MS / 1000,
		regionSec,
		rate,
		semitones: key.semitones,
		masterTempo: key.masterTempo,
		...loopWindow(slot.beats, repeat, regionSec)
	};
}

/** Seconds one pass lasts on the output clock. */
export function passSecOf(
	plan: Pick<VoicePlan, 'rate' | 'loop' | 'loopStartSec' | 'loopEndSec' | 'regionSec'>
): number {
	const span = plan.loop ? plan.loopEndSec - plan.loopStartSec : plan.regionSec;
	return span / plan.rate;
}

/** Plain-source arguments in clip seconds: start offset, play-once duration (null = loop), loop points. */
export function plainSourceArgs(plan: VoicePlan): {
	offsetSec: number;
	durationSec: number | null;
	loopStartSec: number;
	loopEndSec: number;
} {
	return {
		offsetSec: plan.guardSec,
		durationSec: plan.loop ? null : plan.regionSec,
		loopStartSec: plan.guardSec + plan.loopStartSec,
		loopEndSec: plan.guardSec + plan.loopEndSec
	};
}

/** The stretcher schedule a launch sends (loop points 0 = no loop). */
export function stretchLaunchChange(plan: VoicePlan): Required<StretchScheduleChange> {
	return {
		active: true,
		input: plan.guardSec,
		rate: plan.rate,
		semitones: plan.semitones,
		loopStart: plan.loop ? plan.guardSec + plan.loopStartSec : 0,
		loopEnd: plan.loop ? plan.guardSec + plan.loopEndSec : 0
	};
}

/** Output time a play-once stretch voice goes silent, or null while it loops. */
export function stretchStopAt(at: number, plan: VoicePlan): number | null {
	return plan.loop ? null : at + plan.regionSec / plan.rate;
}
