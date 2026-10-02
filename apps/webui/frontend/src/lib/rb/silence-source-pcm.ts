/**
 * Source PCM at the transport playhead, for the master silence watchdog (issue
 * #4030). Master-bus RMS can be near zero during a track's own silent intro or
 * outro; comparing against the held decode buffer distinguishes that from a
 * real dropout (loud source, quiet master).
 *
 * The mixer is the other honest reason for a quiet master: a deck pre-cued in
 * the headphones with its channel fader down, or parked on the far side of the
 * crossfader, is playing and claims live while nothing of it reaches the master
 * bus. So each deck's source RMS is scaled by its linear gain to the master
 * tap before it is compared with the floor.
 */

import { SILENCE_RMS_FLOOR } from '$lib/rb/silence-watchdog';

/** Linear gain below which a deck's path to the master bus is closed, -80 dBFS
 * (same floor as `SILENCE_GAIN_EPSILON` in `master-election.ts`). */
const CLOSED_PATH_GAIN = 1e-4;

export interface SilenceSourceDeckSnap {
	claims_live: boolean;
	buffer: AudioBuffer | null;
	/** Track timeline seconds; same basis as deck transport position. */
	position_sec: number;
	/**
	 * Linear gain from this deck's source to the master analyser tap (trim x
	 * fader x crossfader x master, 0 when every stem part is gained to zero).
	 * Trim can boost above 1, so an absent reading is unknown, not "open", and
	 * never explains silence.
	 */
	master_path_gain?: number;
}

const DEFAULT_WINDOW_SEC = 0.5;

/** RMS 0..1 of mixed channels over a short window from position_sec. NaN when
 * the window starts outside the decoded buffer, and non-finite when the PCM
 * holds NaN or Infinity: an invalid measurement is returned as such, never as
 * silence, so the caller can refuse to read it as content. */
export function rmsAtPlayhead(
	buffer: AudioBuffer,
	position_sec: number,
	window_sec: number = DEFAULT_WINDOW_SEC
): number {
	if (!Number.isFinite(position_sec) || !Number.isFinite(window_sec) || window_sec <= 0) {
		throw new RangeError(
			`position_sec and window_sec must be finite with window_sec > 0, got ${position_sec}, ${window_sec}`
		);
	}
	const sampleRate = buffer.sampleRate;
	const start = Math.floor(position_sec * sampleRate);
	if (start < 0 || start >= buffer.length) return Number.NaN;
	const windowSamples = Math.max(1, Math.floor(window_sec * sampleRate));
	const count = Math.min(windowSamples, buffer.length - start);
	if (count <= 0) return 0;
	let sum = 0;
	let samples = 0;
	for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
		const data = buffer.getChannelData(ch);
		for (let i = 0; i < count; i++) {
			const v = data[start + i] ?? 0;
			sum += v * v;
			samples++;
		}
	}
	return Math.sqrt(sum / samples);
}

/**
 * True when the claimed-live decks' expected contribution to the master bus is
 * below floor: the sum over those decks of master-path gain x playhead RMS. The
 * sum bounds the RMS of the mixed signal from above, so a quiet master next to
 * a quiet expectation is content, never a cut. A deck whose path is closed
 * (fader down, crossfader cut, master at zero, every stem muted) contributes 0
 * without its PCM being read. Everything this cannot measure fails safe toward
 * reporting a real dropout, never toward hiding one:
 * - an absent, non-finite or negative path gain does not explain silence;
 * - non-finite PCM in the window does not explain silence;
 * - an open-path claimed-live deck with no buffer does not explain silence;
 * - a non-finite or negative playhead does not explain silence (and must not throw: a throw
 *   here would skip the fold in `noteMasterSilence`, so the watchdog would stop
 *   counting for as long as the bad position lasted);
 * - a playhead at or past the decoded end does not explain silence: a deck still
 *   claiming live there is a stuck transport, which is exactly what the
 *   watchdog's honest stop exists to catch.
 */
export function claimedLiveSourceIsSilent(
	decks: readonly SilenceSourceDeckSnap[],
	floor: number = SILENCE_RMS_FLOOR,
	window_sec: number = DEFAULT_WINDOW_SEC
): boolean {
	const live = decks.filter((deck) => deck.claims_live);
	if (live.length === 0) return false;
	let expected = 0;
	for (const deck of live) {
		const gain = deck.master_path_gain;
		if (gain === undefined || !Number.isFinite(gain) || gain < 0) return false;
		if (gain < CLOSED_PATH_GAIN) continue;
		if (deck.buffer === null) return false;
		if (!Number.isFinite(deck.position_sec) || deck.position_sec < 0) return false;
		if (deck.position_sec * deck.buffer.sampleRate >= deck.buffer.length) return false;
		const rms = rmsAtPlayhead(deck.buffer, deck.position_sec, window_sec);
		if (!Number.isFinite(rms)) return false;
		expected += gain * rms;
		if (expected >= floor) return false;
	}
	return true;
}
