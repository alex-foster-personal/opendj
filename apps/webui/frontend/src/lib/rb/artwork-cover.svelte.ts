/** LIBM-185: the one way every cover `<img>` outside the library table handles
 * a failed load (deck column, deck header, wave summary, Trackify, inspector,
 * preview bar, dedup). A failure shows the placeholder, is retried on the
 * ArtworkRetry schedule (LIBM-181) with a cache-buster, and is retried at once
 * when the library announces a track change (enrichment fetched a cover).
 * The 404 of a track with no cover yet is expected: mark the `<img>` with
 * `data-expected-resource-error` (COVER_IMG_ATTRS) so it is not a client error.
 *
 *   {#if coverVisible(id)}
 *     <img src={coverSrc(url, id)} data-expected-resource-error
 *          onload={() => coverLoaded(id)} onerror={() => coverFailed(id)} />
 *   {:else}<placeholder />{/if}
 */

import { artworkRetry, artworkRetrySrc } from './artwork-retry';

let tick = $state(0);
let wired = false;

/** Re-render readers whenever any cover's retry state changes. Wired on first read. */
function _wire(): void {
	if (wired) return;
	wired = true;
	artworkRetry().subscribe(() => {
		tick += 1;
	});
}

/** True unless the cover is waiting out a retry delay or was given up. */
export function coverVisible(stableId: string): boolean {
	_wire();
	void tick;
	return !artworkRetry().hidden(stableId);
}

/** The cover URL with the retry cache-buster appended after a failure. */
export function coverSrc(url: string, stableId: string): string {
	_wire();
	void tick;
	return artworkRetrySrc(url, stableId);
}

export function coverLoaded(stableId: string): void {
	artworkRetry().noteLoad(stableId);
}

export function coverFailed(stableId: string): void {
	artworkRetry().noteError(stableId);
}
