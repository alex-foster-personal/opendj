/**
 * Boot pane selection - what the first browser pane shows at launch.
 *
 * Pure, runeless and importless, split out of ./pane-contract.svelte.ts for
 * the same reason `$lib/rb/health-boot-retry` was (the karaoke lyric column
 * plus its re-exports pushed that file past the 600-line file-size gate).
 * pane-contract re-exports the two decisions BrowserPanel.svelte calls, so
 * that component keeps ONE import site for the pane vocabulary; the All
 * Tracks identity and its type are read straight from here by the one suite
 * that asserts on them.
 */

/**
 * The remembered identity of a pane's playlist, small enough to persist.
 *
 * Deliberately NOT a whole PlaylistNode: track_count and children go stale
 * between sessions, and restoring a stale count would put a wrong number on
 * screen before the real load lands. Only the identity survives; everything
 * else is re-fetched.
 */
export interface BootPlaylistChoice {
	playlist_id: string;
	name: string;
	kind: 'all_tracks' | 'playlist';
}

/** The All Tracks identity. Synthesized client-side; the API never returns it. */
export const ALL_TRACKS_CHOICE: BootPlaylistChoice = {
	playlist_id: 'all',
	name: 'All Tracks',
	kind: 'all_tracks'
};

/**
 * What the first pane should show at boot.
 *
 * The pane used to boot at playlist_id=null, so every launch of /performance
 * opened on an empty track table until a human clicked a playlist. That blank
 * pane was indistinguishable from a broken load, which is the honest-state
 * failure this resolves.
 *
 * Rules, in order:
 * - Empty library -> null. There is nothing truthful to show, and defaulting
 *   to All Tracks would render an empty table that looks like a failed load.
 *   The caller keeps its explicit empty state.
 * - A remembered playlist that still exists -> that playlist.
 * - A remembered playlist that is gone (deleted or filtered out of the tree
 *   since last session) -> All Tracks, never a dangling id that would load
 *   into an error.
 * - Nothing remembered -> All Tracks.
 *
 * Pure so the decision is unit-testable without a DOM or a live library.
 */
export function resolveBootPlaylist(args: {
	remembered: BootPlaylistChoice | null;
	known_playlist_ids: readonly string[];
	all_tracks_count: number;
}): BootPlaylistChoice | null {
	if (!Number.isFinite(args.all_tracks_count) || args.all_tracks_count <= 0) return null;
	const remembered = args.remembered;
	if (remembered === null) return ALL_TRACKS_CHOICE;
	if (remembered.kind === 'all_tracks') return ALL_TRACKS_CHOICE;
	if (args.known_playlist_ids.includes(remembered.playlist_id)) return remembered;
	return ALL_TRACKS_CHOICE;
}

/**
 * Whether a background library refresh should retry the boot pane restore.
 *
 * `_refreshLibraryRowsOnce`'s per-pane loop skips any pane whose
 * `playlist_id` is still null ("a blank pane has nothing to refresh"), so a
 * boot pane `_restoreBootPane()` left unclaimed - because the coalesced
 * health read it saw was a stale, falsely-empty snapshot (see
 * request-coalescer.ts's `forceInFlight: false` on the bus's first-ever
 * open) - is never retried by anything else. This is that retry decision,
 * kept pure and separate from `_refreshLibraryRowsOnce` so it is
 * unit-testable through a real module load rather than a text-sliced copy of
 * the Svelte component (PR #1656 review round 7).
 *
 * Retry whenever the boot pane is still unclaimed, UNLESS a pending Spotify
 * deep link owns that blank state on purpose: `_init()` never calls
 * `_restoreBootPane()` at all when `source === 'spotify'` with a selected
 * id, so a still-null playlist_id there means the selection was not found
 * (`spotifyPendingError`) - a deliberate error state a background refresh
 * must not clobber with an arbitrary local playlist.
 */
export function shouldRetryBootPane(args: {
	boot_pane_playlist_id: string | null;
	source: 'collection' | 'spotify';
	spotify_selected_id: string | null;
}): boolean {
	if (args.boot_pane_playlist_id !== null) return false;
	return !(args.source === 'spotify' && args.spotify_selected_id !== null);
}
