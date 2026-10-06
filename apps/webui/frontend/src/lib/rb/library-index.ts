/**
 * The browser's local library index (LIBM-171).
 *
 * rekordbox, Serato, Traktor and djay all hold the library in memory once and
 * resolve every list view from it, so switching views is instant. Before this,
 * All Tracks re-walked ~23 cursor pages (11,165 rows on silver, 35 to 60 s) on
 * every switch back to it. Now the whole listing is ONE request,
 * `GET /api/v1/tracks/index`, held here until the library changes.
 *
 * Staleness is explicit: a library change calls `invalidateLibraryIndex()`,
 * after which the held copy is still served for an instant first paint (and
 * `libraryIndexIsCurrent()` says false) while the next `loadLibraryIndex()`
 * fetches a current one. Concurrent loads for one generation share a request.
 * No $state here, so node:test loads it directly.
 */

import { fetchTrackIndex, type TrackIndexItemWire, type TrackIndexWire } from './api-rb';

export interface LibraryIndex {
	readonly revision: string;
	readonly items: readonly TrackIndexItemWire[];
	readonly generation: number;
}

type FetchTrackIndex = () => Promise<TrackIndexWire>;

let fetchIndex: FetchTrackIndex = fetchTrackIndex;
let generation = 0;
let held: LibraryIndex | null = null;
let inflight: { generation: number; promise: Promise<LibraryIndex> } | null = null;

/** The held index, current or not; null before the first load lands. */
export function peekLibraryIndex(): LibraryIndex | null {
	return held;
}

/** Whether the held index reflects every library change seen so far. */
export function libraryIndexIsCurrent(): boolean {
	return held !== null && held.generation === generation;
}

/** A library change happened: the next load fetches, the held copy stays for paint. */
export function invalidateLibraryIndex(): void {
	generation += 1;
}

/** The current index: the held one, or one shared request for it. A failed
 * request rejects and leaves the held copy (and its staleness) as it was. */
export function loadLibraryIndex(): Promise<LibraryIndex> {
	if (held !== null && held.generation === generation) return Promise.resolve(held);
	if (inflight !== null && inflight.generation === generation) return inflight.promise;
	const wanted = generation;
	const promise = fetchIndex()
		.then((wire) => {
			const index: LibraryIndex = { revision: wire.revision, items: wire.items, generation: wanted };
			if (held === null || held.generation <= wanted) held = index;
			return index;
		})
		.finally(() => {
			if (inflight?.generation === wanted) inflight = null;
		});
	inflight = { generation: wanted, promise };
	return promise;
}

/** Test seam: a fake index fetch without mocking api-rb. */
export function setFetchTrackIndexForTests(fn: FetchTrackIndex | null): void {
	fetchIndex = fn ?? fetchTrackIndex;
}

/** Test-only reset of the held index and its generation. */
export function resetLibraryIndexForTests(): void {
	generation = 0;
	held = null;
	inflight = null;
	fetchIndex = fetchTrackIndex;
}
