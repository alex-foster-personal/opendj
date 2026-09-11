/**
 * Compact autolist inverted index (issue #2066).
 *
 * Worker-safe, no DOM. Union within group via Set union; intersection across
 * groups via Set intersection on posting lists (O(posting), not O(n) rescan).
 */
import {
	BPM_GE200_ID,
	BPM_LT60_ID,
	BPM_UNSPECIFIED_ID,
	GENRE_UNSPECIFIED_ID,
	RATING_UNRATED_ID,
	type AutolistGroupId,
	type AutolistSelection
} from './autolist-rule';

export interface AutolistIndexRow {
	stable_id: string;
	genre: string | null;
	rating: number | null;
	bpm: number | null;
}

export interface AutolistBucket {
	id: string;
	label: string;
	count?: number | undefined;
}

export interface AutolistIndex {
	genre: Map<string, Set<string>>;
	rating: Map<string, Set<string>>;
	bpm: Map<string, Set<string>>;
	order: string[];
}

export function bpmBucketKey(bpm: number | null): string {
	if (bpm === null) return BPM_UNSPECIFIED_ID;
	if (bpm < 60) return BPM_LT60_ID;
	if (bpm >= 200) return BPM_GE200_ID;
	const lo = Math.floor(bpm / 10) * 10;
	return `${lo}-${lo + 9}`;
}

export function ratingBucketKey(rating: number | null): string {
	if (rating === null || rating === 0) return RATING_UNRATED_ID;
	return String(rating);
}

function genreKey(genre: string | null): string {
	return genre ?? GENRE_UNSPECIFIED_ID;
}

function addToMap(map: Map<string, Set<string>>, key: string, id: string): void {
	let set = map.get(key);
	if (set === undefined) {
		set = new Set();
		map.set(key, set);
	}
	set.add(id);
}

export function buildAutolistIndex(rows: AutolistIndexRow[]): AutolistIndex {
	const genre = new Map<string, Set<string>>();
	const rating = new Map<string, Set<string>>();
	const bpm = new Map<string, Set<string>>();
	const order: string[] = [];
	for (const row of rows) {
		order.push(row.stable_id);
		addToMap(genre, genreKey(row.genre), row.stable_id);
		addToMap(rating, ratingBucketKey(row.rating), row.stable_id);
		addToMap(bpm, bpmBucketKey(row.bpm), row.stable_id);
	}
	return { genre, rating, bpm, order };
}

function unionIds(map: Map<string, Set<string>>, keys: string[]): Set<string> | null {
	if (keys.length === 0) return null;
	let out: Set<string> | null = null;
	for (const key of keys) {
		const posting = map.get(key) ?? new Set();
		if (out === null) out = new Set(posting);
		else for (const id of posting) out.add(id);
	}
	return out ?? new Set();
}

function intersectSets(a: Set<string>, b: Set<string>): Set<string> {
	const out = new Set<string>();
	for (const id of a) {
		if (b.has(id)) out.add(id);
	}
	return out;
}

export function queryAutolistIndex(index: AutolistIndex, selection: AutolistSelection): string[] {
	let result: Set<string> | null = null;
	const groups: AutolistGroupId[] = ['genre', 'rating', 'bpm'];
	const maps = { genre: index.genre, rating: index.rating, bpm: index.bpm };
	for (const group of groups) {
		const keys = selection[group] ?? [];
		const groupSet = unionIds(maps[group], keys);
		if (groupSet === null) continue;
		result = result === null ? groupSet : intersectSets(result, groupSet);
	}
	if (result === null) return [];
	const orderIndex = new Map(index.order.map((id, i) => [id, i]));
	return [...result].sort((a, b) => (orderIndex.get(a) ?? 0) - (orderIndex.get(b) ?? 0));
}

export function genreBucketsFromIndex(index: AutolistIndex): AutolistBucket[] {
	const keys = [...index.genre.keys()].filter((k) => k !== GENRE_UNSPECIFIED_ID).sort();
	const out: AutolistBucket[] = keys.map((k) => {
		const bucket: AutolistBucket = { id: k, label: k };
		const size = index.genre.get(k)?.size;
		if (size !== undefined) bucket.count = size;
		return bucket;
	});
	const unspec = index.genre.get(GENRE_UNSPECIFIED_ID);
	if (unspec !== undefined && unspec.size > 0) {
		out.push({ id: GENRE_UNSPECIFIED_ID, label: 'Unspecified', count: unspec.size });
	} else if (out.length === 0) {
		out.push({ id: GENRE_UNSPECIFIED_ID, label: 'Unspecified', count: 0 });
	}
	return out;
}

export function staticRatingBuckets(): AutolistBucket[] {
	const out: AutolistBucket[] = [];
	for (let n = 5; n >= 1; n -= 1) {
		out.push({ id: String(n), label: n === 1 ? '1 star' : `${n} stars` });
	}
	out.push({ id: RATING_UNRATED_ID, label: 'Unrated' });
	return out;
}

export function staticBpmBuckets(): AutolistBucket[] {
	const out: AutolistBucket[] = [{ id: BPM_LT60_ID, label: '<60' }];
	for (let lo = 60; lo < 200; lo += 10) {
		out.push({ id: `${lo}-${lo + 9}`, label: `${lo}-${lo + 9}` });
	}
	out.push({ id: BPM_GE200_ID, label: '>=200' });
	out.push({ id: BPM_UNSPECIFIED_ID, label: 'Unspecified' });
	return out;
}
