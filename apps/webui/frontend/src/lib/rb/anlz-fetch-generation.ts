/**
 * Monotonic counter embedded in every /anlz request's query string and
 * in-flight dedupe key (api-rb.ts), bumped once per rbx-vs-own analysis
 * source switch (anlz-cache.svelte.ts refreshAnalysisSourceDecks).
 *
 * PARITY-02's switch changes what the SAME /anlz URL returns
 * (beatgrid_source). The backend marked a decoded response
 * `Cache-Control: public, max-age=3600` when this was written; it is now
 * `private, no-cache` with an ETag over the body (rb_assets.py _CACHE_ANLZ,
 * discussion_r3970967302), which closes the same gap SERVER-side for callers
 * that never see this counter at all.
 * refreshAnalysisSourceDecks re-fetches only the decks that are
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

/** Per-track revision of what /anlz serves, bumped when a write changes one
 * track's payload (GRIDEDIT-6235c: a grid edit). A fetch issued under an older
 * revision may carry the pre-edit grid, so it must not publish over the
 * refresh that follows the edit; `fetchAnlz` also folds it into its in-flight
 * dedupe key, so a fetch issued after the edit never joins one from before. */
const _trackRevision = new Map<string, number>();

export function anlzTrackRevision(stable_id: string): number {
	return _trackRevision.get(stable_id) ?? 0;
}

/** Bump `stable_id`'s revision and return the new one. */
export function bumpAnlzTrackRevision(stable_id: string): number {
	const revision = anlzTrackRevision(stable_id) + 1;
	_trackRevision.set(stable_id, revision);
	return revision;
}
