/**
 * ARTTHUMB-8728c: the pure half of the playlist art option, safe for
 * presentational components and their stories (no fetch, no store).
 * The flag and pref state live in library-sidebar-art.svelte.ts.
 */
import { API_BASE } from '$lib/api/base';

export const ART_THUMBNAILS_FLAG_ID = 'library.art_thumbnails';
/** Variant A (the maintainer, Wed 4 Nov 2026): 48 px tiles, 4 px corners, two-line by-line. */
export const SIDEBAR_TILE_PX = 48;
export type ThumbnailSize = 64 | 232;

export function sidebarArtEnabledIn(flags: readonly { flag_id: string; enabled: boolean }[]): boolean {
	return flags.find((flag) => flag.flag_id === ART_THUMBNAILS_FLAG_ID)?.enabled === true;
}

export function playlistThumbnailUrl(playlistId: string, size: ThumbnailSize): string {
	return `${API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}/thumbnail?size=${size}`;
}
