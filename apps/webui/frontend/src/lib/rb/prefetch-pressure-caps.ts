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

/** Scaled prefetch track cap for the current pressure state. */
export function pressureScaledPrefetchTrackCap(tierCap: number): number {
	return pressureScaledCap(tierCap, SCALERS.LOW.prefetch_tracks, _tracksState);
}

/** Scaled prefetch byte cap for the current pressure state. */
export function pressureScaledPrefetchByteCap(tierCap: number): number {
	return pressureScaledCap(tierCap, SCALERS.LOW.prefetch_bytes, _bytesState);
}

/** Test-only: reset to baseline (steps = 0) between cases. */
export function resetPressureScaledPrefetchCaps(): void {
	_tracksState = createPressureCapState();
	_bytesState = createPressureCapState();
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
		xrunsAtPreviousTick = options.readXruns();
		options.applyCaps();
	});
}
