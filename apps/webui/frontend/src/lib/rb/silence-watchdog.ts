/**
 * P0 (defect D2): "the deck says it is playing" and "signal is leaving the
 * master bus" are two different claims, and nothing compared them.
 *
 * On Wed 2 Sep 2026 audio stopped at 12:58 CEST and the decks went on
 * reporting themselves as playing for ~24 minutes. `peekDeckMeterReading()` reads a
 * real per-deck AnalyserNode RMS, but it feeds a cosmetic VFader pulse and
 * nothing else. The sibling ledger item `deck-fully-silent-while-playing`
 * recorded the same symptom from the other direction (gain reaching zero) in
 * round 1 and it was still unfixed.
 *
 * A PURE FOLD, not a class owning a timer, for the same reason `xrun-math.ts`
 * is pure: the thing worth testing is the decision, and a decision observable
 * only by waiting out a real timer is a decision nobody can test on a thrashing
 * laptop. The caller supplies the clock.
 *
 * EDGE-TRIGGERED. The verdict appears on the sample that crosses the window and
 * on no other, so one dropout is one toast rather than one per meter read.
 */

/** Sustained silence longer than this, under a playing deck, is a dropout. */
export const SILENT_WHILE_PLAYING_MS = 2_000;

/**
 * Below this RMS counts as silence.
 *
 * Not zero: a live graph carries denormals and dither, and an exact-zero test
 * would miss a dropout that leaves a whisper of noise behind. Well under the
 * 0.02 the VFader already treats as "no visible signal".
 */
export const SILENCE_RMS_FLOOR = 0.001;

export type SilenceVerdict = 'ok' | 'silent-while-playing' | 'output-stalled-while-rendering';

export interface SilenceSample {
	playing: boolean;
	audible?: boolean;
	/** A separate monitor path is intentionally carrying the live deck while
	 * the room/master bus is silent. This is normal DJ cueing, not a dropout. */
	masterOutputIntentionallySilent?: boolean;
	masterRms: number;
	tMs: number;
}

export interface SilenceState {
	/** When the current silent run began, or null if there is signal. */
	silentSinceMs: number | null;
	/** Whether this run has already been reported. */
	reported: boolean;
	lastTMs: number | null;
	verdict: SilenceVerdict;
}

const EMPTY: Readonly<SilenceState> = Object.freeze({
	silentSinceMs: null,
	reported: false,
	lastTMs: null,
	verdict: 'ok' as SilenceVerdict
});

export function foldSilenceSample(
	state: Readonly<SilenceState> = EMPTY,
	sample: SilenceSample
): SilenceState {
	const { playing, masterRms, tMs, audible, masterOutputIntentionallySilent } = sample;
	const claimedLive = (playing || audible === true) && masterOutputIntentionallySilent !== true;
	// A broken meter must not read as silence: it would raise a dropout alarm
	// through perfectly good audio, which is worse than having no watchdog.
	if (!Number.isFinite(masterRms) || masterRms < 0) {
		throw new RangeError(`masterRms must be a finite non-negative number, got ${masterRms}`);
	}
	if (!Number.isFinite(tMs)) {
		throw new RangeError(`tMs must be a finite number, got ${tMs}`);
	}
	if (state.lastTMs !== null && tMs < state.lastTMs) {
		throw new RangeError(
			`sample time went backwards (${tMs} after ${state.lastTMs}); the elapsed-silence ` +
				'arithmetic would go negative and the window would never elapse'
		);
	}
	const silent = claimedLive && masterRms < SILENCE_RMS_FLOOR;
	if (!silent) {
		return { silentSinceMs: null, reported: false, lastTMs: tMs, verdict: 'ok' };
	}
	const since = state.silentSinceMs ?? tMs;
	const crossed = !state.reported && tMs - since >= SILENT_WHILE_PLAYING_MS;
	return {
		silentSinceMs: since,
		reported: state.reported || crossed,
		lastTMs: tMs,
		verdict: crossed ? 'silent-while-playing' : 'ok'
	};
}
