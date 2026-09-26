/**
 * Beatgrid-fallback gate + read model for one WaveRow (analysis-router lane).
 */
import type { AnlzData } from '$lib/rb/anlz-types';
import type { BeatgridFallbackOut } from '$lib/rb/beatgrid-fallback-api';
import { resolvePaintAnlz, shouldUseBeatgridFallback, type BeatgridFallbackGate } from '$lib/rb/beatgrid-fallback';
import type { BeatgridFallbackEntry } from './beatgrid-fallback-cache.svelte';

export function beatgridFallbackGate(input: {
	anlzErrorCode: string | null;
	anlz: AnlzData | null;
	effectiveSource: BeatgridFallbackGate['effectiveSource'];
}): BeatgridFallbackGate {
	return {
		anlzErrorCode: input.anlzErrorCode,
		anlz: input.anlz,
		vendor: null,
		effectiveSource: input.effectiveSource
	};
}

export function readyBeatgridFallback(
	stable_id: string | null,
	gate: ReturnType<typeof beatgridFallbackGate>,
	getEntry: (stable_id: string) => BeatgridFallbackEntry | undefined
): BeatgridFallbackOut | null {
	if (stable_id === null || !shouldUseBeatgridFallback(gate)) return null;
	const entry = getEntry(stable_id);
	return entry !== undefined && entry.status === 'ready' ? entry.data : null;
}

export function paintAnlzForRow(anlz: AnlzData | null, fallback: BeatgridFallbackOut | null): AnlzData | null {
	return resolvePaintAnlz(anlz, fallback);
}
