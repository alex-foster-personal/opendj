/**
 * Track artwork URLs, as a leaf module.
 *
 * `api-rb.ts` re-exports these builders, so existing importers are unchanged.
 * They live here so a component that only draws a cover (the Crate Desk's
 * preview bar and inspector) need not import the whole rekordbox API client:
 * api-rb is the frontend's most-imported module (frontend.max_fan_in).
 */

import { API_BASE } from '$lib/api/base';
import type { ArtworkSize } from './library-types';
import { trackApiPath } from './track-source';

/** URL for GET /tracks/{sid}/artwork - use directly as <img src>. The
 * backend 404s ARTWORK_NOT_FOUND; consumers render the gray placeholder
 * slate on img error, never a fabricated image. */
export function artworkUrl(stable_id: string, size: ArtworkSize = 's'): string {
	return `${API_BASE}${trackApiPath(stable_id, `/artwork?size=${size}`)}`;
}

/** Artwork for a loaded deck: the same chain as {@link artworkUrl}, and when
 * the track has no local artwork (rekordbox, embedded picture, folder image)
 * the server also looks it up on MusicBrainz + Cover Art Archive and caches
 * what it finds. Only decks ask for this: the lookup is held to one request
 * per second, so a library page of rows must never trigger it. */
export function deckArtworkUrl(stable_id: string, size: ArtworkSize = 's'): string {
	return `${artworkUrl(stable_id, size)}&online=true`;
}
