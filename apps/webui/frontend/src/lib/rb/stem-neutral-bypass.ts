/**
 * AUDFID-02: a deck with a ready stem bundle plays the ORIGINAL file until a
 * stem control is first touched.
 *
 * Measured Tue 3 Nov 2026 (Tank, vocals/other 64 kbps, drums/bass 96 kbps,
 * source 253 kbps): summing four lossy stems through four time-stretchers
 * leaves a residual only 4 dB below source above 8 kHz and costs 0.5 to 1.4 dB;
 * the maintainer heard it as "tinny". The stems are still built and held, and the deck
 * still reads stems `ready` (buttons live); only the audible processor is the
 * mix until the first touch, which hands over with a crossfade.
 *
 * Decisions:
 *   - EQ needs no switch: the deck EQ (lowshelf / peaking / highshelf) sits on
 *     the channel graph after whichever processor plays, so it shapes the
 *     original file exactly as it shapes the stems. `stem_eq_mode` is a flag
 *     whose effects arrive as stem control writes, which do switch.
 *   - Returning every control to neutral does NOT switch back: the deck stays
 *     on stems until the next load. A back-and-forth would cost a crossfade per
 *     touch and a second stretcher for the whole play.
 *
 *   ✔︎ ✅ 🎯 Neutral controls play the original file.
 *       [if] a ready bundle with default controls lands on a deck [then ⛔️] it is held, not made audible
 *   ✔︎ ✅ 🎯 The first touch crossfades on one timeline, at least 10 ms.
 *       [if] the crossfade is shorter than 10 ms or the ramps start at different instants [then ⛔️]
 */
import { createDefaultStemControls, STEM_CONTROLS } from '$lib/rb/stem-graph';
import type { MixerChannelState } from '$lib/rb/mixer-types';
import type { StemControls } from '$lib/rb/stem-graph';
import type { StemDeckState } from '$lib/rb/stem-types';

/** Which processor a deck with a ready bundle is playing. */
export type StemPlayback = 'original' | 'stems';

/** Crossfade from the mix processor to the stems on the first touch. 20 ms:
 * twice the 10 ms floor (AUDFID-02), and short enough that two phase-aligned
 * renderings of the same audio never read as a doubled hit. */
export const STEM_ENGAGE_CROSSFADE_SEC = 0.02;
export const STEM_ENGAGE_CROSSFADE_MIN_SEC = 0.01;

/** EQ knob centre: the channel EQ is flat at 0.5 on every band. */
export const EQ_FLAT = 0.5;

/** True when every stem control is at its default (unmuted, unsoloed, unity). */
export function stemControlsAreDefault(controls: StemControls): boolean {
	const neutral = createDefaultStemControls();
	return STEM_CONTROLS.every((stem) => {
		const state = controls[stem];
		const want = neutral[stem];
		return state.muted === want.muted && state.solo === want.solo && state.gain === want.gain;
	});
}

/** True when the deck EQ is flat on all three bands. */
export function channelEqIsFlat(
	channel: Pick<MixerChannelState, 'eq_low' | 'eq_mid' | 'eq_high'>
): boolean {
	return channel.eq_low === EQ_FLAT && channel.eq_mid === EQ_FLAT && channel.eq_high === EQ_FLAT;
}

/**
 * The AUDFID-02/03 neutral state: every stem control untouched and the EQ flat.
 * Stable signature, also read by the live fidelity guard (AUDFID-03). A deck
 * with no ready bundle is neutral when its EQ is flat (it plays the file).
 */
export function stemControlsNeutral(
	stems: Pick<StemDeckState, 'status' | 'controls'>,
	channel: Pick<MixerChannelState, 'eq_low' | 'eq_mid' | 'eq_high'>
): boolean {
	const controlsNeutral = stems.status === 'ready' ? stemControlsAreDefault(stems.controls) : true;
	return controlsNeutral && channelEqIsFlat(channel);
}

/** True when a deck must keep its ready bundle held and play the original. */
export function deckPlaysOriginalFile(controls: StemControls): boolean {
	return stemControlsAreDefault(controls);
}

export interface CrossfadeParam {
	setValueAtTime(value: number, time: number): unknown;
	linearRampToValueAtTime(value: number, time: number): unknown;
}

/** Schedule the stems in and the mix out over one window on one clock. Both
 * ramps share `when` and `when + sec`, so the sum stays at unity for two
 * phase-aligned renderings of the same audio. */
export function scheduleStemEngageCrossfade(
	incoming: CrossfadeParam,
	outgoing: CrossfadeParam | null,
	when: number,
	sec: number
): { startSec: number; endSec: number } {
	if (!Number.isFinite(when) || when < 0)
		throw new RangeError(`crossfade start must be a finite time, got ${when}`);
	if (!Number.isFinite(sec) || sec < STEM_ENGAGE_CROSSFADE_MIN_SEC) {
		throw new RangeError(
			`stem engage crossfade must be at least ${STEM_ENGAGE_CROSSFADE_MIN_SEC}s, got ${sec}`
		);
	}
	const end = when + sec;
	incoming.setValueAtTime(0, when);
	incoming.linearRampToValueAtTime(1, end);
	if (outgoing !== null) {
		outgoing.setValueAtTime(1, when);
		outgoing.linearRampToValueAtTime(0, end);
	}
	return { startSec: when, endSec: end };
}
