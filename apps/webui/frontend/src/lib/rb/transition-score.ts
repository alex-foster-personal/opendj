/**
 * Incoming LP low-kill timeline policy (issue #13335).
 *
 * Filter-lane transitions keep incoming `eq_low` killed until the bass-hit
 * boundary (`swap_at` in `(private path)`).
 * Knob values mirror autodj `KILL` (0) and `FLAT` (0.5) in `apps/autodj/styles.py`.
 */

import {
	bassEntryMs,
	type AutoPlayNextConfig,
	DEFAULT_AUTO_PLAY_NEXT_CONFIG
} from './auto-play-next';
import type { AnlzBeat, AnlzWaveform } from './anlz-types';

/** Knob value matching autodj KILL (eq low fully out). */
export const INCOMING_LOW_KILL_KNOB = 0;

/** Knob value matching autodj FLAT (eq low open at hit). */
export const INCOMING_LOW_RELEASE_KNOB = 0.5;

/**
 * Incoming low EQ knob for LP filter lanes before/after the bass-hit boundary.
 * Half-open pre-hit interval: [0, boundary) is kill; [boundary, +inf) is release.
 */
export function incomingLowKnobAtMs(positionMs: number, bassHitBoundaryMs: number | null): number {
	if (!Number.isFinite(positionMs)) {
		throw new RangeError('positionMs must be finite');
	}
	if (bassHitBoundaryMs === null) {
		return INCOMING_LOW_KILL_KNOB;
	}
	if (!Number.isFinite(bassHitBoundaryMs) || bassHitBoundaryMs < 0) {
		throw new RangeError('bassHitBoundaryMs must be a non-negative finite number or null');
	}
	if (positionMs < bassHitBoundaryMs) {
		return INCOMING_LOW_KILL_KNOB;
	}
	return INCOMING_LOW_RELEASE_KNOB;
}

/** Bass-hit boundary for incoming low-kill timing; delegates to `bassEntryMs`. */
export function resolveBassHitBoundaryMs(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	durationSec: number,
	fromMs: number,
	config: AutoPlayNextConfig = DEFAULT_AUTO_PLAY_NEXT_CONFIG
): number | null {
	return bassEntryMs(waveform, beats, durationSec, fromMs, config);
}
