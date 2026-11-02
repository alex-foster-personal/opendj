/**
 * CUEOUT-6060 entry point the preview click loads on demand.
 *
 * `preview-cue.svelte.ts` sits in the library page's initial bundle, which has
 * a gzip budget (`scripts/check-bundle-size.sh`). The snap planner and its
 * live read only run for a 'phase' preview, so the click path imports this
 * module dynamically, alongside the audio fetch, instead of shipping it on
 * first paint. It also owns the clicked track's grid fetch.
 */

import { previewStartFor, type PreviewStart } from '$lib/player/preview-beat-snap';
import { previewMasterRead } from '$lib/player/preview-beat-snap-live';
import { PREVIEW_TEMPO_RANGE_PCT } from '$lib/player/preview-beat-sync';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { validateBeatGrid } from '$lib/rb/beat-sync-math';
import { anlzTrackRevision, currentAnlzFetchGeneration } from '$lib/rb/anlz-fetch-generation';

/**
 * Least-recently-used map with a count cap. Insert and read both promote the
 * key to newest; a put past the cap drops the oldest. Pure, so the cap and its
 * eviction order are tested on plain values.
 */
export class BoundedLru<K, V> {
	readonly #max: number;
	readonly #map = new Map<K, V>();

	constructor(max: number) {
		if (!Number.isInteger(max) || max < 1) throw new RangeError(`max must be a positive integer, got ${max}`);
		this.#max = max;
	}

	get size(): number {
		return this.#map.size;
	}

	get(key: K): V | undefined {
		const value = this.#map.get(key);
		if (value === undefined) return undefined;
		this.#map.delete(key);
		this.#map.set(key, value);
		return value;
	}

	put(key: K, value: V): void {
		this.#map.delete(key);
		this.#map.set(key, value);
		for (const oldest of this.#map.keys()) {
			if (this.#map.size <= this.#max) break;
			this.#map.delete(oldest);
		}
	}

	clear(): void {
		this.#map.clear();
	}
}

/** A held grid, stamped with the analysis source it was read under. A switch
 * between own and Rekordbox analysis bumps the generation, and a track's
 * re-analysis bumps its revision, so either makes the entry stale. */
interface HeldGrid {
	grid: readonly AnlzBeat[] | null;
	generation: number;
	revision: number;
}

/**
 * Beat grids of previewed tracks, least recently used first. Warm-on-hover
 * fills this for every dwelt row, so it is capped by count like the PCM cache
 * is capped by bytes, and the preview's idle release clears it.
 */
export const PREVIEW_GRID_CACHE_MAX = 32;
const _grids = new BoundedLru<string, HeldGrid>(PREVIEW_GRID_CACHE_MAX);

/** The clicked track's beat grid, or null when it has none or it cannot be
 * read. Never throws: a missing grid only means the preview does not snap.
 * `fetchAnlz` is handed in by the caller, which already imports the API
 * module, so this chunk adds no importer to it.
 *
 * Only a settled answer is cached. A fetch that throws is transient (network,
 * a payload still being analysed), so the next call asks again. A fetch that
 * settles after the analysis source changed is returned to its caller but not
 * stored, because the grid it read may belong to the previous source. */
export async function previewGridFor(
	stable_id: string,
	fetchAnlz: (stable_id: string) => Promise<{ beatgrid?: { beats?: readonly AnlzBeat[] } | null }>
): Promise<readonly AnlzBeat[] | null> {
	const generation = currentAnlzFetchGeneration();
	const revision = anlzTrackRevision(stable_id);
	const held = _grids.get(stable_id);
	if (held !== undefined && held.generation === generation && held.revision === revision) {
		return held.grid;
	}
	let beats: readonly AnlzBeat[];
	try {
		beats = (await fetchAnlz(stable_id)).beatgrid?.beats ?? [];
	} catch {
		return null;
	}
	let grid: readonly AnlzBeat[] | null;
	try {
		validateBeatGrid(beats);
		grid = beats;
	} catch {
		grid = null;
	}
	if (generation === currentAnlzFetchGeneration() && revision === anlzTrackRevision(stable_id)) {
		_grids.put(stable_id, { grid, generation, revision });
	}
	return grid;
}

/** How many grids are held; for the cap's unit test. */
export function previewGridCount(): number {
	return _grids.size;
}

/** Drop every held grid (the preview's idle or explicit release). */
export function releasePreviewGrids(): void {
	_grids.clear();
}

/** Read the master now and decide a 'phase' preview's start. Synchronous, so
 * the caller can plan and `start()` in one task. */
export function previewPhaseStart(input: {
	previewGrid: readonly AnlzBeat[] | null;
	trackBpm: number | null;
	clickSec: number;
	nowContextSec: number;
}): PreviewStart {
	const masterRead = previewMasterRead();
	return previewStartFor({
		mode: 'phase',
		masterBpm: masterRead.masterBpm,
		master: masterRead.master,
		masterRefusal: masterRead.refusal,
		previewGrid: input.previewGrid,
		trackBpm: input.trackBpm,
		clickSec: input.clickSec,
		nowContextSec: input.nowContextSec,
		pitchRangePct: PREVIEW_TEMPO_RANGE_PCT
	});
}
