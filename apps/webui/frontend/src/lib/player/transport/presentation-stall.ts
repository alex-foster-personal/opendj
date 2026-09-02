/**
 * The belt to the braces in `presentation.ts`: somebody watches the number
 * being PAINTED.
 *
 * `presentation.ts` detects and works around the one diagnosed cause of a
 * frozen playhead (a stale `getOutputTimestamp().contextTime`). This module
 * catches a frozen playhead whatever caused it, including causes nobody has
 * diagnosed - which is the general shape of both defects recorded on
 * Wed 2 Sep 2026. In each one the app held every fact it needed and nobody
 * looked.
 *
 * WaveRow gated its rAF on `deck.playing`, which is desired INTENT
 * (`st.playing = rt.desiredActive`), not presented truth, so it repainted a
 * pixel-identical frame at 60Hz for twenty minutes and called that healthy.
 *
 * A pure fold, edge-triggered, for the same reasons as `silence-watchdog.ts`.
 */

/**
 * How long the painted position may stand still, while playing, before it is
 * called frozen. Matches `PRESENTATION_STALL_MS` in intent but is measured on
 * the OUTPUT of the presentation path rather than its input, so the two can
 * catch different things and must not be collapsed into one constant.
 */
export const PRESENTATION_STALL_MS = 750;

export type PresentationVerdict = 'ok' | 'presentation-stalled';

export interface PresentationSample {
	playing: boolean;
	position_ms: number;
	tMs: number;
}

export interface PresentationStallState {
	frozenSinceMs: number | null;
	lastPositionMs: number | null;
	reported: boolean;
	lastTMs: number | null;
	verdict: PresentationVerdict;
}

const EMPTY: Readonly<PresentationStallState> = Object.freeze({
	frozenSinceMs: null,
	lastPositionMs: null,
	reported: false,
	lastTMs: null,
	verdict: 'ok' as PresentationVerdict
});

export function foldPresentationSample(
	state: Readonly<PresentationStallState> = EMPTY,
	sample: PresentationSample
): PresentationStallState {
	const { playing, position_ms: positionMs, tMs } = sample;
	// An unreadable position would compare equal to the last one forever and
	// raise a freeze alarm through perfectly healthy playback.
	if (!Number.isFinite(positionMs)) {
		throw new RangeError(`position_ms must be a finite number, got ${positionMs}`);
	}
	if (!Number.isFinite(tMs)) {
		throw new RangeError(`tMs must be a finite number, got ${tMs}`);
	}
	if (state.lastTMs !== null && tMs < state.lastTMs) {
		throw new RangeError(
			`sample time went backwards (${tMs} after ${state.lastTMs}); the elapsed arithmetic ` +
				'would go negative and the window would never elapse'
		);
	}
	// A stopped playhead not moving is the definition of working correctly.
	if (!playing) {
		return {
			frozenSinceMs: null,
			lastPositionMs: positionMs,
			reported: false,
			lastTMs: tMs,
			verdict: 'ok'
		};
	}
	if (state.lastPositionMs === null || positionMs !== state.lastPositionMs) {
		return {
			frozenSinceMs: null,
			lastPositionMs: positionMs,
			reported: false,
			lastTMs: tMs,
			verdict: 'ok'
		};
	}
	const since = state.frozenSinceMs ?? tMs;
	const crossed = !state.reported && tMs - since >= PRESENTATION_STALL_MS;
	return {
		frozenSinceMs: since,
		lastPositionMs: positionMs,
		reported: state.reported || crossed,
		lastTMs: tMs,
		verdict: crossed ? 'presentation-stalled' : 'ok'
	};
}
