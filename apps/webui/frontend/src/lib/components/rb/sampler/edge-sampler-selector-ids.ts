/**
 * Keyboard selector ids for the edge sampler (GitHub issue #13706, epic #9372).
 *
 * The sampler is a fixed overlay outside every page section, so each screen edge is a
 * root selector group (`sampler.l` / `sampler.r`) whose numbered pads are leaves
 * (`sampler.l.pad1` ...). Ids are plain strings, so `selector-register.ts` is untouched.
 */
import type { Edge } from '$lib/player/sampler/sampler-types';

/** Returns the selector group id of one screen edge of the sampler. */
export function edgeSamplerSelectorId(edge: Edge): string {
	return `sampler.${edge.toLowerCase()}`;
}

/** Returns the selector leaf id of the pad at zero-based index on one edge (pad numbers start at 1). */
export function edgeSamplerPadSelectorId(edge: Edge, index: number): string {
	if (!Number.isInteger(index) || index < 0)
		throw new Error(`Invalid edge sampler pad index: ${index}`);
	return `${edgeSamplerSelectorId(edge)}.pad${index + 1}`;
}
