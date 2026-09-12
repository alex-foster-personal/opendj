import type { DeckId } from '$lib/rb/deck-id';
import type { PlaylistNode } from '$lib/rb/library-types';
import type { SmartlistSummary } from '$lib/rb/api-smartlists';
import type { ColumnTrackRow } from './ColumnBrowser.svelte';

/** Shared props for PlaylistTree and LibraryNav (forwards the same contract). */
export type PlaylistTreeProps = {
	nodes: PlaylistNode[];
	/** Pin e0f3a90652a9: names/counts is a cheap read, but available_count
	 * is a real per-member disk-existence pass and can take a couple of
	 * seconds cold (first load after app focus). Required, not optional --
	 * an unwired caller must fail the Svelte type check rather than silently
	 * render as if already loaded (a missed wire previously read as a
	 * healthy empty playlist panel, the exact silent-success failure the
	 * pin was about). */
	playlistsLoading: boolean;
	playlistsError: string | null;
	allTracksCount: number | null;
	allTracksBrokenCount: number | null;
	allTracksError: string | null;
	selectedId: string | null;
	/** The active pane's PaneStore.selected_id, forwarded to ColumnBrowser
	 * so its track highlight reflects the current pane rather than an
	 * independent selection that would desync on pane switches. Distinct
	 * from `selectedId` above, which is the selected PLAYLIST id. */
	trackSelectedId: string | null;
	/** Pin 2ac3a0: playlists holding a track currently loaded into a deck
	 * (light blue tint) and playlists open in 2+ pane tabs at once
	 * (darker blue tint) - both derived by BrowserPanel from ALREADY
	 * HYDRATED pane rows only. Absent = no tinting (e.g. an integrator
	 * that hasn't wired deck/pane context yet). */
	deckLoadedPlaylistIds?: ReadonlySet<string>;
	multiPanePlaylistIds?: ReadonlySet<string>;
	onselect: (node: PlaylistNode) => void;
	/** Optional until the browser integrator wires smartlist selection
	 * into BrowserPanel; absent = smartlist rows render inert. */
	onselectsmartlist?: (smartlist: SmartlistSummary) => void;
	/** Column View lane callbacks - optional, same absent-means-inert
	 * convention as onselectsmartlist (ColumnBrowser itself no-ops a
	 * click/dblclick with no handler wired). */
	onselecttrack?: (row: ColumnTrackRow) => void;
	onloadtrack?: (row: ColumnTrackRow, deck: DeckId | null) => void;
	/** Create then return new playlist_id (or null on cancel/fail). */
	oncreateplaylist?: () => Promise<string | null> | string | null;
	/** Commit in-place rename; empty/cancelled name leaves server name. */
	onrenameplaylist?: (node: PlaylistNode, name: string) => void | Promise<void>;
	ondeleteplaylist?: (node: PlaylistNode) => void;
	onduplicateplaylist?: (node: PlaylistNode) => void;
	/**
	 * Library tracks dropped onto a playlist row. Absent = rows are not
	 * drop targets, same absent-means-inert convention as above.
	 */
	ondroptracks?: (playlistId: string, stableIds: string[]) => void;
};
