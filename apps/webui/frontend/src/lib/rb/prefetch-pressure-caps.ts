/**
 * PERFMODE-04 Q29 driver: steps the audio prefetch cache caps down under
 * pressure and back up once it clears, paired with the audio-prefetch-cache-
 * caps shed job (playing-gate.ts) that defers the fetch pump itself.
 *
 * Dependency-injected on purpose (isPlaying / pressureElevated / readXruns /
 * applyCaps), matching createPlayingGate and createBackgroundDemandShed: the
 * real signals live behind anyDeckPlaying and machine-pressure, both of
 * which pull in audio-engine.svelte.ts, so wiring this module to them
 * directly would import that graph here too. app-init.ts supplies the real
 * functions; tests supply fakes.
 */

import { S1_XRUN_DELTA_MAX } from '$lib/rb/machine-pressure';
import { SCALERS } from '$lib/rb/perf-tier';
import {
	createPressureCapState,
	pressureScaledCap,
	stepPressureCapState,
	type PressureCapState
} from '$lib/rb/pressure-cap-scaling';

let _tracksState: PressureCapState = createPressureCapState();
let _bytesState: PressureCapState = createPressureCapState();
let _previewBytesState: PressureCapState = createPressureCapState();

/** Scaled prefetch track cap for the current pressure state. */
export function pressureScaledPrefetchTrackCap(tierCap: number): number {
	return pressureScaledCap(tierCap, SCALERS.LOW.prefetch_tracks, _tracksState);
}

/** Scaled prefetch byte cap for the current pressure state. */
export function pressureScaledPrefetchByteCap(tierCap: number): number {
	return pressureScaledCap(tierCap, SCALERS.LOW.prefetch_bytes, _bytesState);
}

/** Scaled decoded-preview byte cap for the current pressure state (CUEOUT-15). */
export function pressureScaledPreviewPcmByteCap(tierCap: number): number {
	return pressureScaledCap(tierCap, SCALERS.LOW.preview_pcm_bytes, _previewBytesState);
}

/**
 * Has the preview cap stepped down from baseline?
 *
 * The preview's hover warm reads this to decide whether to SKIP a speculative
 * decode, rather than defer it the way the eager stem decode does. A deferred
 * warm is worthless: by the time signals clear the operator has already
 * clicked or moved on, so the owed work would land as pure cost. Reusing the
 * stepper's own state rather than re-deriving the pressure signal keeps one
 * closed loop in the page and means the warm resumes as the cap steps back up.
 */
export function previewWarmIsShed(): boolean {
	return _previewBytesState.steps > 0;
}

/** Test-only: reset to baseline (steps = 0) between cases. */
export function resetPressureScaledPrefetchCaps(): void {
	_tracksState = createPressureCapState();
	_bytesState = createPressureCapState();
	_previewBytesState = createPressureCapState();
}

export interface ArmPrefetchPressureCapScalingOptions {
	isPlaying: () => boolean;
	pressureElevated: () => boolean;
	readXruns: () => number;
	/** Re-run LRU eviction against the new caps; production passes applyPrefetchCaps. */
	applyCaps: () => void;
	subscribe: (onTick: () => void) => () => void;
}

/** Arm the pressure-driven cap stepper for the page lifetime. Returns teardown. */
export function armPrefetchPressureCapScaling(options: ArmPrefetchPressureCapScalingOptions): () => void {
	let xrunsAtPreviousTick = options.readXruns();
	return options.subscribe(() => {
		const xrunWindowElevated = options.readXruns() - xrunsAtPreviousTick > S1_XRUN_DELTA_MAX;
		const elevated = options.isPlaying() && (options.pressureElevated() || xrunWindowElevated);
		_tracksState = stepPressureCapState(_tracksState, elevated);
		_bytesState = stepPressureCapState(_bytesState, elevated);
		_previewBytesState = stepPressureCapState(_previewBytesState, elevated);
		xrunsAtPreviousTick = options.readXruns();
		options.applyCaps();
	});
}
