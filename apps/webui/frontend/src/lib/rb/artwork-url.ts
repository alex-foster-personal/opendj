/**
 * Track artwork URLs, as a leaf module.
 *
 * `api-rb.ts` re-exports both builders, so existing importers are unchanged.
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
