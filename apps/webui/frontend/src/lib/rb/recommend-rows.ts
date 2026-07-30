/**
 * Map suggest-next candidates → BrowserRow appendage for the open playlist.
 * These are ordinary library rows rendered by TrackTable (same columns /
 * virtualization). They only appear after scrolling past playlist members.
 */
import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';

export interface SuggestCandidate {
	stable_id: string;
	title: string | null;
	artist: string | null;
	bpm: number | null;
	key_camelot: string | null;
	energy: number | null;
	rating: number | null;
	rationale_tags: string[];
	explain_text: string | null;
}

/** Candidates not already in the open playlist, as BrowserRows after `orderBase`. */
export function recommendedBrowserRows(
	candidates: readonly SuggestCandidate[],
	memberIds: ReadonlySet<string>,
	orderBase: number
): BrowserRow[] {
	const out: BrowserRow[] = [];
	let order = orderBase;
	for (const c of candidates) {
		if (memberIds.has(c.stable_id)) continue;
		order += 1;
		const tags = (c.rationale_tags ?? []).filter((t) => typeof t === 'string' && t.length > 0);
		out.push({
			stable_id: c.stable_id,
			order,
			title: c.title,
			artist: c.artist,
			key: c.key_camelot,
			bpm: c.bpm,
			rating: c.rating,
			etag: '',
			comments: tags.length > 0 ? tags.join(' · ') : (c.explain_text ?? 'recommended'),
			duration_ms: null,
			genre: null,
			file_exists: true,
			play_count: 0,
			is_streaming: null,
			strip: null,
			vocals: { status: 'not_analyzed' },
			hot_cues: [],
			rb_meta: null,
			revealed: false,
			match_context: null,
			row_kind: 'recommended'
		});
	}
	return out;
}
