/**
 * FXFILT-09 (issue #10010): the channel FILTER's per-side section chain.
 *
 * At 12 dB/oct a side is one biquad, wired straight into its wet gain, which
 * is the shipped graph node for node. At 24 a second section of the same type
 * sits between them at the same corner with a flat Butterworth Q, so the first
 * section's corner bump is unchanged and the rolloff doubles. Every corner of
 * a side rides one glide (`filterSideCorners`). Mirrored in `apps/audio-engine/src/deck.rs`.
 */
import { FILTER_SECOND_SECTION_Q_DB, type FilterSlopeDbOct } from '$lib/player/constants';

/** Wire one FILTER side's first section into its wet gain; at 24 dB/oct the second section between them is returned. */
export function wireFilterSide(
	ctx: BaseAudioContext,
	first: BiquadFilterNode,
	wet: AudioNode,
	slope: FilterSlopeDbOct
): BiquadFilterNode | null {
	switch (slope) {
		case 12:
			first.connect(wet);
			return null;
		case 24: {
			const second = ctx.createBiquadFilter();
			second.type = first.type;
			second.Q.value = FILTER_SECOND_SECTION_Q_DB;
			second.frequency.value = first.frequency.value;
			first.connect(second);
			second.connect(wet);
			return second;
		}
		default: {
			const _exhaustive: never = slope;
			throw new Error(`FILTER slope must be 12 or 24 dB/oct, got ${_exhaustive}`);
		}
	}
}

/** Every corner param of one FILTER side, first section first; `second` is absent or null at 12 dB/oct. */
export function filterSideCorners(
	first: BiquadFilterNode,
	second: BiquadFilterNode | null | undefined
): AudioParam[] {
	return second == null ? [first.frequency] : [first.frequency, second.frequency];
}
