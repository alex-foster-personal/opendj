/**
 * Monotonic counter embedded in every /anlz request's query string and
 * in-flight dedupe key (api-rb.ts), bumped once per rbx-vs-own analysis
 * source switch (audio-engine.svelte.ts refreshDecksForAnalysisSourceChange).
 *
 * PARITY-02's switch changes what the SAME /anlz URL returns
 * (beatgrid_source), but the backend marks a decoded response
 * `Cache-Control: public, max-age=3600` (rb_assets.py _CACHE_ANLZ).
 * refreshDecksForAnalysisSourceChange re-fetches only the decks that are
 * CURRENTLY loaded; a track merely prefetched into the browser's own HTTP
 * cache (library row hover, BrowserPanel selection, an unloaded deck slot)
 * is untouched by that loop, so a later `load()` of it via the plain
 * `fetchAnlz` path could still be served the pre-switch payload straight out
 * of the browser cache, with no network round trip and no code here even
 * running (discussion_r3921839825 - the earlier per-deck-only fix left this
 * gap). Folding the generation into the URL makes every fetch issued after a
 * switch address a query string the browser has never cached, for every
 * caller, not just the decks this module knows about - the same fix
 * `invalidateAllAnlzCacheEntries` already applies to the app-level cache,
 * extended to the one layer beneath it that a cache-entry eviction cannot
 * reach.
 */
let _generation = 0;

/** Current generation, folded into an /anlz query string and dedupe key. */
export function currentAnlzFetchGeneration(): number {
	return _generation;
}

/** Bump on every analysis-source switch, regardless of direction - a return
 * to a previously-seen source (own -> rekordbox -> own) must not reuse the
 * first "own" fetch's cached URL either, since nothing here tracks whether
 * the underlying own-analysis record changed in between. */
export function bumpAnlzFetchGeneration(): void {
	_generation += 1;
}
