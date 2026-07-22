/**
 * Column view (browser-surface unit, Miller-column lane): the pure
 * artist -> album -> track derivation ColumnBrowser.svelte renders.
 *
 * Artist/album (not genre) because this is a LIBRARY-WIDE browse: the
 * bulk /tracks listing contract (TrackListItemWire, api-rb.ts) carries
 * artist + album on every row (base Track fields) but does NOT carry
 * genre - genre is only inline on playlist-hydrated rows (contract 4) or
 * behind the lazy per-track rb-meta fetch. A genre column sourced from
 * the bulk listing would be null on every real row - that is exactly the
 * "no mocked data ever" house rule forbidding a column that LOOKS real
 * but never resolves.
 *
 * Selector convention (matches the 3-state real-data-only shape used
 * elsewhere in the browser lane): undefined = no filter ('All'), null =
 * the explicit "missing value" bucket, a string = that exact value.
 * null is a REAL bucket - it is never silently folded into 'All'.
 */

export type ColumnSelector = string | null | undefined;

/** Minimal per-track shape this module needs. */
export interface ColumnSourceRow {
	artist: string | null;
	album: string | null;
}

export interface ColumnBucket {
	/** null = the 'missing value' bucket. */
	value: string | null;
	count: number;
}

function _bucketsBy<T extends ColumnSourceRow>(
	rows: T[],
	keyOf: (row: T) => string | null
): ColumnBucket[] {
	const counts = new Map<string | null, number>();
	for (const row of rows) {
		const key = keyOf(row);
		counts.set(key, (counts.get(key) ?? 0) + 1);
	}
	const buckets = [...counts.entries()].map(([value, count]) => ({ value, count }));
	// Real values alphabetical (case-insensitive); the missing-value bucket
	// (null) always sinks last, mirroring sortRows' null-last convention.
	buckets.sort((a, b) => {
		if (a.value === null && b.value === null) return 0;
		else if (a.value === null) return 1;
		else if (b.value === null) return -1;
		else return a.value.localeCompare(b.value);
	});
	return buckets;
}

/** Distinct artists (+ counts) across rows - the first Miller column. */
export function artistBuckets<T extends ColumnSourceRow>(rows: T[]): ColumnBucket[] {
	return _bucketsBy(rows, (r) => r.artist);
}

/** Distinct albums (+ counts) among rows matching `artist` - the second
 * Miller column, narrowed by the first column's selection. */
export function albumBuckets<T extends ColumnSourceRow>(
	rows: T[],
	artist: ColumnSelector
): ColumnBucket[] {
	return _bucketsBy(_filterByArtist(rows, artist), (r) => r.album);
}

function _filterByArtist<T extends ColumnSourceRow>(rows: T[], artist: ColumnSelector): T[] {
	if (artist === undefined) return rows;
	return rows.filter((r) => r.artist === artist);
}

/** The final Miller column's row set: rows matching both selections. */
export function filterByColumn<T extends ColumnSourceRow>(
	rows: T[],
	artist: ColumnSelector,
	album: ColumnSelector
): T[] {
	const byArtist = _filterByArtist(rows, artist);
	if (album === undefined) return byArtist;
	return byArtist.filter((r) => r.album === album);
}
