/**
 * Playlist deck-context tints + the tree's CURRENT fold control (pin 2ac3a0).
 *
 * Split out of pane-contract.svelte.ts: these are pure derivations over
 * ALREADY HYDRATED pane state (never a new fetch of an unopened playlist
 * just to colour it in), not part of the PaneStore contract itself. Kept
 * plain .ts (no runes used) so it loads under the same node:test harness
 * as its sibling.
 */

// Deliberately no import of PaneStore from ./pane-contract.svelte: that
// module re-exports this one (see the bottom of pane-contract.svelte.ts),
// so importing its PaneStore type back here would recreate the same
// import cycle these derivations were split out to avoid. Each function
// below instead takes only the structural slice of a pane it actually
// reads - narrower than PaneStore, and satisfied by it without change.

// ---------------------------------------------- playlist deck-context tints
// Pin 2ac3a0: a non-selected playlist row tints blue when it is somehow
// "already open" elsewhere - lightly when a track loaded into a deck came
// from it, darker when it is open in 2+ pane tabs at once. Both are derived
// from panes' ALREADY HYDRATED rows only (never a new fetch of an unopened
// playlist just to colour it in - that would need a new backend query and
// is explicitly out of scope for this pin).

/** Which tint (if any) a playlist row should render. Selected always wins -
 * the existing selection colour takes precedence over both tints - and
 * `multi` (2+ open tabs) outranks `deck` (a loaded-deck track) when both
 * apply, since it is the rarer, more surprising state to miss. */
export type PlaylistTint = 'none' | 'deck' | 'multi';

export function playlistTintOf(args: {
	playlist_id: string;
	selected: boolean;
	deckLoadedPlaylistIds: ReadonlySet<string>;
	multiPanePlaylistIds: ReadonlySet<string>;
}): PlaylistTint {
	if (args.selected) return 'none';
	if (args.multiPanePlaylistIds.has(args.playlist_id)) return 'multi';
	if (args.deckLoadedPlaylistIds.has(args.playlist_id)) return 'deck';
	return 'none';
}

/** Playlists (excluding All Tracks / blank panes) holding at least one row
 * whose stable_id is currently loaded into a deck. */
export function derivePlaylistDeckMembership(
	panes: readonly { playlist_id: string | null; rows: readonly { stable_id: string }[] }[],
	deckLoadedStableIds: ReadonlySet<string>
): Set<string> {
	const out = new Set<string>();
	for (const pane of panes) {
		if (pane.playlist_id === null || pane.playlist_id === 'all') continue;
		if (pane.rows.some((r) => deckLoadedStableIds.has(r.stable_id))) out.add(pane.playlist_id);
	}
	return out;
}

/** How many open pane tabs currently show each playlist (excluding All
 * Tracks / blank panes). */
export function derivePlaylistPaneOpenCounts(
	panes: readonly { playlist_id: string | null }[]
): Map<string, number> {
	const out = new Map<string, number>();
	for (const pane of panes) {
		if (pane.playlist_id === null || pane.playlist_id === 'all') continue;
		out.set(pane.playlist_id, (out.get(pane.playlist_id) ?? 0) + 1);
	}
	return out;
}

/** Playlists open in 2 or more pane tabs at once, from the open-count map. */
export function multiPanePlaylistIds(counts: ReadonlyMap<string, number>): Set<string> {
	const out = new Set<string>();
	for (const [id, n] of counts) {
		if (n >= 2) out.add(id);
	}
	return out;
}

// ------------------------------------------------------- CURRENT fold control
// Pin 2ac3a0: mirrors TrackTable's MASTER-with-chevron fold (browser/TrackTable.svelte
// masterFold/jumpToMaster) - when the selected playlist row scrolls outside the
// tree's own viewport, a sticky blue CURRENT control with an up/down chevron
// appears at the corresponding edge so a library with MANY playlists never loses
// track of which one is open. Pure position math, same edge tolerance (2px) and
// above/below rule as masterFold, so it is unit-testable without mounting the tree.

export function computeTreeCurrentFold(args: {
	/** Selected row's offsetTop/offsetBottom within the scrollable tree
	 * content; null when the selected playlist has no rendered row (e.g. a
	 * smartlist, or nothing selected). */
	selectedTop: number | null;
	selectedBottom: number | null;
	scrollTop: number;
	viewportHeight: number;
}): 'above' | 'below' | null {
	const { selectedTop, selectedBottom, scrollTop, viewportHeight } = args;
	if (selectedTop === null || selectedBottom === null || viewportHeight <= 0) return null;
	if (selectedBottom <= scrollTop + 2) return 'above';
	if (selectedTop >= scrollTop + viewportHeight - 2) return 'below';
	return null;
}
