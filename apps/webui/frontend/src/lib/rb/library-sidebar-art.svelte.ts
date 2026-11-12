/**
 * ARTTHUMB-8728c: the "playlist art in the sidebar" library view option.
 *
 * Two gates, both needed to show tiles: the `library.art_thumbnails` build
 * flag (default ON) and the disk-backed ui-pref `library_sidebar_art`
 * (default ON, PUT /api/v1/ui-prefs reads and changes it too). A leaf module
 * so the playlist tree and the Settings toggle read it without importing the
 * prefs singleton. Works in every skin: the tile URL is
 * /api/v1/playlists/{id}/thumbnail, which the artwork grayscale rule
 * (img[src*='/artwork']) does not match, so no skin grays it unless it opts in.
 *
 * Requirements (mini-PRD):
 *   ✔︎ sidebarArtEnabledIn(): true only when the daemon declares the flag on.
 *     [if] flags are unread or the flag is absent [then ⛔️ tiles show]
 *   ✔︎ playlistThumbnailUrl(): encodes the id and pins the size to 64 or 232;
 *     an optional third argument appends revision as query param v (PlaylistSummary.updated_at).
 *     [if] the url contains /artwork [then ⛔️ a skin grays it by accident]
 */
import { buildFlags } from '$lib/api/store-build.svelte';
import { api } from '$lib/api/rb-http';
import { sidebarArtEnabledIn } from './library-sidebar-art-url';

export {
	ART_THUMBNAILS_FLAG_ID,
	SIDEBAR_TILE_PX,
	sidebarArtEnabledIn,
	playlistThumbnailUrl,
	type ThumbnailSize
} from './library-sidebar-art-url';

export const SIDEBAR_ART_PREF = 'library_sidebar_art';

export const sidebarArt = $state({ pref: true });

/** Flag on AND the view option on. */
export function sidebarArtShown(): boolean {
	void buildFlags.load();
	return sidebarArtEnabledIn(buildFlags.flags) && sidebarArt.pref;
}

/** Flag only: whether the option itself (and the /playlists grid link) is offered. */
export function artThumbnailsAvailable(): boolean {
	void buildFlags.load();
	return sidebarArtEnabledIn(buildFlags.flags);
}

export function hydrateSidebarArtFromDisk(body: { library_sidebar_art?: boolean }): void {
	if (typeof body.library_sidebar_art === 'boolean') sidebarArt.pref = body.library_sidebar_art;
}

/** Set the option; rejects when the daemon refuses, so the toggle never shows a value it did not save. */
export async function setSidebarArt(next: boolean): Promise<void> {
	const previous = sidebarArt.pref;
	sidebarArt.pref = next;
	try {
		await api.PUT('/api/v1/ui-prefs', { body: { library_sidebar_art: next } });
	} catch (error) {
		sidebarArt.pref = previous;
		throw error;
	}
}
