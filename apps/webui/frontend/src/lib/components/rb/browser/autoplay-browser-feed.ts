/**
 * BrowserPanel AutoPlay feed observation: maps rendered browser rows into the
 * snapshot module without mounting Svelte. Unit tests replay config-vs-rows boot
 * order through the same path the panel $effect uses (issue #700, PLAY-06).
 */
import type { AutoPlayFeedDecision, AutoPlayFeedSnapshot } from '$lib/rb/autoplay-feed';
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';

export type AutoPlayBrowserRowLike = {
	stable_id: string;
	edit_id?: string | null;
	in_ms?: number | null;
	out_ms?: number | null;
	is_edit?: boolean;
	key: string | null;
	bpm: number | null;
	file_exists?: boolean | null;
	has_beatgrid?: boolean;
	title?: string | null;
	artist?: string | null;
};

export function autoPlayTrackRowsFromBrowser(
	renderedRows: readonly AutoPlayBrowserRowLike[]
): AutoPlayTrackRow[] {
	return renderedRows.map((r) => ({
		stable_id: r.stable_id,
		...(r.edit_id == null ? {} : { edit_id: r.edit_id }),
		...(r.in_ms == null ? {} : { in_ms: r.in_ms }),
		...(r.out_ms == null ? {} : { out_ms: r.out_ms }),
		...(r.is_edit === true ? { is_edit: true } : {}),
		key: r.key,
		bpm: r.bpm,
		file_exists: r.file_exists === true,
		...(typeof r.has_beatgrid === 'boolean' ? { has_beatgrid: r.has_beatgrid } : {}),
		...(r.title === undefined ? {} : { title: r.title }),
		...(r.artist === undefined ? {} : { artist: r.artist })
	}));
}

export function stepAutoPlayBrowserFeed(
	feed: AutoPlayFeedSnapshot,
	input: {
		enabled: boolean;
		playlistId: string | null;
		renderedRows: readonly AutoPlayBrowserRowLike[];
		filterKey?: string;
		membershipHydrating?: boolean;
	}
): AutoPlayFeedDecision {
	return feed.step(
		input.enabled,
		input.playlistId,
		autoPlayTrackRowsFromBrowser(input.renderedRows),
		input.filterKey ?? '',
		input.membershipHydrating ?? false
	);
}
