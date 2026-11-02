/**
 * SETNOTE-6340e/06: the debounced track search behind the `@` popover and the
 * right-click menu. It is the Cmd K collection search (GET /api/v1/search,
 * artist, title and partial words), reached through the store's searchTracks.
 */
import { searchTracks, type TrackHit } from './store.svelte';

const DEBOUNCE_MS = 150;

export function hitLabel(hit: TrackHit): string {
	return [hit.artist, hit.title].filter(Boolean).join(' - ') || hit.stable_id;
}

export function createTrackSearch(limit: number) {
	const state = $state({ hits: [] as TrackHit[], error: null as string | null, searched: false });
	let timer: ReturnType<typeof setTimeout> | null = null;
	let seq = 0;

	function cancel(): void {
		if (timer !== null) clearTimeout(timer);
		timer = null;
		seq++;
	}

	async function _run(q: string, mine: number): Promise<void> {
		try {
			const hits = await searchTracks(q, limit);
			if (mine !== seq) return;
			Object.assign(state, { hits, error: null, searched: true });
		} catch (error) {
			if (mine !== seq) return;
			Object.assign(state, { hits: [], error: error instanceof Error ? error.message : String(error), searched: true });
		}
	}

	/** Replaces any pending search; an empty query clears the hits. */
	function query(raw: string, immediate = false): void {
		cancel();
		const q = raw.trim();
		state.searched = false;
		if (!q) {
			Object.assign(state, { hits: [], error: null });
			return;
		}
		// The old hits belong to the old query: Enter must not insert one while this one runs.
		Object.assign(state, { hits: [], error: null });
		const mine = seq;
		if (immediate) void _run(q, mine);
		else timer = setTimeout(() => void _run(q, mine), DEBOUNCE_MS);
	}

	return { state, query, cancel };
}
