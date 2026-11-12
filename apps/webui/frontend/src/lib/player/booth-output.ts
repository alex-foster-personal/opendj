/**
 * Booth monitor level and graph gain (#11257). Keeps mixer.booth as the
 * serializable contract while headphones.ts owns the audio graph nodes.
 */

import type { BoothSignalMode } from '$lib/rb/mixer-types';
import { mixerState } from '$lib/player/state.svelte';

export function assertBoothSignalMode(value: unknown): asserts value is BoothSignalMode {
	if (value !== 'off' && value !== 'follows_master' && value !== 'independent') {
		throw new TypeError(
			`booth signal_mode must be off, follows_master, or independent; got ${JSON.stringify(value)}`
		);
	}
}

/** Audible booth gain after `signal_mode`. */
export function effectiveBoothGain(): number {
	const { level, signal_mode } = mixerState.booth;
	if (signal_mode === 'off') return 0;
	if (signal_mode === 'follows_master') return level * mixerState.master;
	return level;
}
