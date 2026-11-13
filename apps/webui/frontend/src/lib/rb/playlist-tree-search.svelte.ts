/**
 * LIBM-87 (issue #12319): the playlist tree's name search.
 *
 * The query is session state shared by the tree's search box and the
 * `browser_tree_search` / `browser_tree_search_clear` performance commands,
 * so a user typing and an agent issuing a command move the same value. It
 * filters the presentation only: the tree's nodes, its selection and the
 * Playlists section's own open state are never written, so clearing the
 * query shows the tree exactly as it was.
 */
import { filterTreeByName } from '$lib/rb/skin-layout';

export const playlistTreeSearch = $state<{ query: string }>({ query: '' });

/** Set the tree query; a blank one clears the search. */
export function setPlaylistTreeSearch(query: string): void {
	if (typeof query !== 'string') throw new TypeError('playlist tree query must be a string');
	playlistTreeSearch.query = query;
}

export interface PlaylistTreeSearchView<T> {
	/** Rows to draw: the matches with their folder path, or the input untouched. */
	nodes: T[];
	/** A non-blank query is filtering the rows. */
	active: boolean;
	/** The search is active and nothing matched. */
	noMatches: boolean;
	/** Whether the Playlists section draws open: forced open while searching. */
	open: boolean;
}

/** What the tree draws for `query`, leaving `nodes` and `playlistsOpen` unchanged. */
export function playlistTreeSearchView<T extends { name: string; children: T[] }>(
	nodes: T[],
	query: string,
	playlistsOpen: boolean
): PlaylistTreeSearchView<T> {
	const active = query.trim() !== '';
	const shown = filterTreeByName(nodes, query);
	return {
		nodes: shown,
		active,
		noMatches: active && shown.length === 0,
		open: active || playlistsOpen
	};
}
