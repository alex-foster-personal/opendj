/**
 * Source PCM at the transport playhead, for the master silence watchdog (issue
 * #4030). Master-bus RMS can be near zero during a track's own silent intro or
 * outro; comparing against the held decode buffer distinguishes that from a
 * real dropout (loud source, quiet master).
 */

import { SILENCE_RMS_FLOOR } from '$lib/rb/silence-watchdog';

export interface SilenceSourceDeckSnap {
	claims_live: boolean;
	buffer: AudioBuffer | null;
	/** Track timeline seconds; same basis as deck transport position. */
	position_sec: number;
}

const DEFAULT_WINDOW_SEC = 0.5;

/** RMS 0..1 of mixed channels over a short window from position_sec. */
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
	if (start < 0 || start >= buffer.length) return 0;
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
	const rms = Math.sqrt(sum / samples);
	return Number.isFinite(rms) ? rms : 0;
}

/**
 * True when every claimed-live deck with a buffer has playhead RMS below floor.
 * A claimed-live deck with no buffer does not explain silence (fail-safe toward
 * reporting a real dropout).
 */
export function claimedLiveSourceIsSilent(
	decks: readonly SilenceSourceDeckSnap[],
	floor: number = SILENCE_RMS_FLOOR,
	window_sec: number = DEFAULT_WINDOW_SEC
): boolean {
	const live = decks.filter((deck) => deck.claims_live);
	if (live.length === 0) return false;
	for (const deck of live) {
		if (deck.buffer === null) return false;
		if (rmsAtPlayhead(deck.buffer, deck.position_sec, window_sec) >= floor) return false;
	}
	return true;
}
