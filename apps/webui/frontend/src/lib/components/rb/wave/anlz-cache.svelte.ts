/**
 * Reactive per-track ANLZ cache for the wavestack (build unit: wavestack).
 *
 * One /anlz fetch per stable_id per session; rows $derive off the entry so
 * they repaint when the payload lands. Backend error codes are kept as REAL
 * row states (ANALYSIS_NOT_FOUND renders the explicit 'no analysis' row -
 * SCREENSHOT-SPEC 7); unexpected failures are recorded AND re-thrown so they
 * surface loudly as unhandled rejections. No silent fallbacks, no invented
 * waveforms.
 *
 * .svelte.ts extension is REQUIRED for the $state rune (RECON-FRONTEND 10.1).
 */
import { fetchAnlz, fetchAnlzBypassingHttpCache, RbApiError } from '$lib/rb/api-rb';
import { hasAnlzBeatgrid } from '$lib/rb/beatgrid-fallback';
import { recordAnlzPrefetchSampled } from '$lib/rb/library-perf';
import { currentAnlzFetchGeneration } from '$lib/rb/anlz-fetch-generation';
import type { AnlzData } from '$lib/rb/anlz-types';
import {
	refreshAnalysisSourceDecks as _refreshAnalysisSourceDecksImpl,
	type AnalysisSourceRefreshDeck,
	type DeckId as _RefreshDeckId
} from './analysis-source-refresh';

export type { AnalysisSourceRefreshDeck };

export { upgradeDeckBeatgrid } from '$lib/player/beatgrid-lazy';
export {
	createBeatgridResyncGuards,
	createBeatgridResyncTracking
} from '$lib/player/beatgrid-resync-guards';
export type { BeatgridResyncPorts } from '$lib/player/beatgrid-resync-guards';
// Re-exported so a caller that already imports this module for the cache
// itself (audio-engine.svelte.ts) can read the fetch generation through the
// same edge, rather than adding a second one to anlz-fetch-generation.ts.
export { currentAnlzFetchGeneration } from '$lib/rb/anlz-fetch-generation';
// Same reasoning: audio-engine.svelte.ts is pinned at the file_size.max_frontend
// and frontend.max_fan_out ratchet floors with zero headroom, so this reads
// the last CONFIRMED rbx-vs-own selection (for createBeatgridResyncGuards's
// desiredBeatgridSource probe, discussion_r3975650988 P1 BLOCKING) through
// the edge that already exists here, rather than a new one straight to the
// leaf module.
export { analysisSourceState } from '$lib/rb/analysis-source-state.svelte';

export type AnlzEntry =
	| { status: 'loading'; generation: number }
	| { status: 'ready'; data: AnlzData; retryAfter?: number }
	| { status: 'error'; code: string };

const _cache = $state<Record<string, AnlzEntry>>({});

/** Floor between refetches of a retryable entry (ms). A stable_id with no
 * decode yet sits behind a reactive $effect (WaveRow.svelte) that reruns on
 * every write to its own cache entry, so without a floor a saturated decoder
 * would be hammered at effect-rerun speed - hardest exactly when it is
 * already overloaded. This bounds it to at most 1 refetch per track per
 * window, which still satisfies "retry once capacity frees" (issue #735
 * follow-up). */
const RETRYABLE_COOLDOWN_MS = 2000;

/** True for a 'ready' entry whose local_waveform is a TRANSIENT not_decoded
 * (decoder momentarily saturated, issue #735 follow-up) - not a terminal
 * answer, the track itself may still decode. Independent of `retryAfter`
 * timing: this is "is this entry the retryable CLASS", not "is it due for
 * retry right now". Everything else cached (a real decode, a permanent
 * not_decoded, any error) is terminal. */
function _isRetryableEntry(entry: AnlzEntry): entry is Extract<AnlzEntry, { status: 'ready' }> {
	if (entry.status !== 'ready') return false;
	if (entry.data.local_waveform?.status !== 'not_decoded') return false;
	return entry.data.local_waveform.retryable === true;
}

/** True once a retryable entry's cooldown has genuinely elapsed - i.e. it is
 * DUE for a refetch right now. Only meaningful for a `_isRetryableEntry`. */
function _isDueForRetry(entry: Extract<AnlzEntry, { status: 'ready' }>): boolean {
	return entry.retryAfter === undefined || performance.now() >= entry.retryAfter;
}

/** True when `ensureAnlz` should fire a fresh fetch for an existing entry:
 * it is the retryable class AND its cooldown has elapsed. Everything else
 * (loading, error, a terminal ready entry, or a retryable entry still
 * inside its cooldown) must not re-trigger a fetch. */
function _dueForEnsureRefetch(entry: AnlzEntry): boolean {
	return _isRetryableEntry(entry) && _isDueForRetry(entry);
}

/** True for a raw (not yet cached) /anlz response whose local_waveform is a
 * transient decoder-saturation reject - the same class `_isRetryableEntry`
 * detects, but usable BEFORE the response has been wrapped in a cache entry
 * (e.g. a deck-load direct fetch inspecting its own result). */
export function isRetryableAnlzData(data: AnlzData): boolean {
	return data.local_waveform?.status === 'not_decoded' && data.local_waveform.retryable === true;
}

/** Unconditionally fetches /anlz and writes the outcome into the cache -
 * shared by `ensureAnlz` (which gates this behind `_dueForEnsureRefetch`
 * for a reactive/effect caller) and the ambient retry timer in
 * `_publishAnlzResult` (which must NOT re-apply that gate: it fires exactly
 * at the cooldown it itself scheduled, so it is due by construction, and
 * re-checking `performance.now() >= retryAfter` against a SECOND, separately
 * read clock sample can lose that comparison by a sub-millisecond hair
 * (`setTimeout`'s own firing jitter vs. the `retryAfter` stamp taken a few
 * microseconds earlier) - confirmed live: this silently killed the retry
 * chain outright, not just delayed it, since the fired-early callback both
 * skips the fetch AND schedules no successor (Codex finding, issue #735
 * follow-up, discussion_r3907928251 fix review).
 *
 * Captures the analysis-source fetch generation (anlz-fetch-generation.ts)
 * at the moment the fetch is ISSUED, not when it settles: a switch mid-flight
 * (PARITY-02) calls `invalidateAllAnlzCacheEntries` to wipe this cache, but an
 * already-in-flight fetch started under the OLD generation has no way to
 * cancel, and would otherwise resurrect a fresh 'ready' entry carrying the
 * pre-switch source right after the wipe - a later cache hit
 * (`isAnlzEntryUsable`) then serves that stale payload to a load/prefetch
 * that happens entirely after the switch (discussion_r3921839825 follow-up).
 * A mismatch at settle time means a newer switch superseded this request:
 * discard the write outright rather than publish it. The discard clears the
 * placeholder this call itself wrote (see `_discardSuperseded` below) rather
 * than leaving it stuck: without that, a superseded prefetch stranded the
 * entry at `status: 'loading'` forever, since `_dueForEnsureRefetch` refuses
 * to retry a loading entry, and the current-source waveform was never
 * requested again until an unrelated deck load happened to overwrite it
 * (discussion_r3975650980 P2 BLOCKING). */
function _fetchAndPublish(stable_id: string): void {
	const generation = currentAnlzFetchGeneration();
	_cache[stable_id] = { status: 'loading', generation };
	const startedAt = performance.now();
	void fetchAnlz(stable_id).then(
		(data: AnlzData) => {
			recordAnlzPrefetchSampled(performance.now() - startedAt, 'ready');
			if (generation !== currentAnlzFetchGeneration()) {
				_discardSuperseded(stable_id, generation);
				return;
			}
			_publishAnlzResult(stable_id, data);
		},
		(err: unknown) => {
			recordAnlzPrefetchSampled(performance.now() - startedAt, 'error');
			if (generation !== currentAnlzFetchGeneration()) {
				_discardSuperseded(stable_id, generation);
				return;
			}
			if (err instanceof RbApiError) {
				// Explicit backend state (e.g. ANALYSIS_NOT_FOUND, 0.1% of tracks).
				_cache[stable_id] = { status: 'error', code: err.code };
				return;
			}
			_cache[stable_id] = { status: 'error', code: 'FETCH_FAILED' };
			throw err; // loud: network/shape failures must not vanish
		}
	);
}

/** Clears a superseded fetch's own `loading` placeholder so the stable_id
 * reads back as never-requested (`ensureAnlz` then treats it as a miss and
 * fetches under the current generation) instead of stuck forever. Only
 * clears the placeholder THIS call wrote: if a newer fetch for the same
 * stable_id already overwrote it - its own later `loading` marker, or an
 * already-settled `ready`/`error` result - that must survive untouched. */
function _discardSuperseded(stable_id: string, generation: number): void {
	const entry = _cache[stable_id];
	if (entry !== undefined && entry.status === 'loading' && entry.generation === generation) {
		delete _cache[stable_id];
	}
}

/** One pending ambient-retry timer per stable_id, so a second `_publishAnlzResult`
 * for the same id (e.g. `ensureAnlz`'s own fetch settling right after a
 * caller's direct one already scheduled a retry) replaces the old timer
 * instead of stacking a second one that would double-fire the next retry. */
const _retryTimers = new Map<string, ReturnType<typeof setTimeout>>();

/** Live consumers per stable_id - a deck currently loaded with it, or a
 * browser row currently selected. The ambient retry timer below only
 * refires while at least one token remains registered; a rapid
 * keyboard/scroll sweep across many uncached rows would otherwise leave
 * every rejected stable_id retrying every cooldown forever, each still
 * holding a decoder slot hostage long after nothing on screen shows it
 * (Codex finding, issue #735 follow-up, discussion_r3908644098). Callers
 * register on demand (WaveRow's deck effect, BrowserPanel's selection
 * effect) and unregister on their own effect cleanup - this module has no
 * visibility into decks or panes itself. */
const _consumers = new Map<string, Set<symbol>>();

/** Registers the caller as still wanting `stable_id`'s anlz kept warm.
 * Returns a token to hand back to `unregisterAnlzConsumer` - callers must
 * pair every register with exactly one unregister (typically an `$effect`
 * cleanup), or the id would read as consumed forever. */
export function registerAnlzConsumer(stable_id: string): symbol {
	const token = Symbol('anlz-consumer');
	let tokens = _consumers.get(stable_id);
	if (tokens === undefined) {
		tokens = new Set();
		_consumers.set(stable_id, tokens);
	}
	tokens.add(token);
	return token;
}

/** Releases a token from `registerAnlzConsumer`. A no-op if the token was
 * already released (e.g. a stale cleanup running after a full reset), so
 * callers never need to guard the call themselves. */
export function unregisterAnlzConsumer(stable_id: string, token: symbol): void {
	const tokens = _consumers.get(stable_id);
	if (tokens === undefined) return;
	tokens.delete(token);
	if (tokens.size === 0) _consumers.delete(stable_id);
}

function _hasActiveConsumer(stable_id: string): boolean {
	const tokens = _consumers.get(stable_id);
	return tokens !== undefined && tokens.size > 0;
}

/** Writes a fetched /anlz result into the shared cache with the same
 * retryable/terminal bookkeeping `ensureAnlz` applies to its own fetch.
 * Module-private: every external caller now goes through `ensureAnlz` or
 * `fetchAnlzForDeckLoad`, which both call this internally, so the cache stays
 * the single place this bookkeeping lives instead of a second, divergent
 * copy at the call site (BrowserPanel's strip adoption used to keep its own
 * copy here and read the cache reactively instead - Codex finding, issue
 * #735 follow-up, discussion_r3908286630).
 *
 * A retryable write SELF-SCHEDULES a follow-up fetch after the cooldown.
 * Without this, "the cooldown elapsed" is not a reactive event - nothing
 * re-runs a Svelte `$effect` just because time passed with no dependency
 * changing, so a caller relying on a component effect to notice the
 * cooldown clearing would never actually retry (Codex finding, issue #735
 * follow-up, discussion_r3907928251). The chain terminates on its own the
 * moment a non-retryable result lands (this function is not called again
 * for it).
 *
 * The scheduled call goes straight to `_fetchAndPublish`, NOT `ensureAnlz` -
 * it must not re-run `_dueForEnsureRefetch`'s time check against a second,
 * separately read clock sample. That check exists to stop a REACTIVE caller
 * (WaveRow's `$effect`) from re-firing mid-cooldown; this timer fires
 * exactly when its own cooldown elapses; re-applying the check anyway
 * chases the same `performance.now()` vs. `retryAfter` race the comment on
 * `_fetchAndPublish` documents, which was caught live silently ending the
 * retry chain.
 *
 * This is now the ONLY retry mechanism for a retryable result - a caller
 * must not ALSO hand-roll its own delayed re-fetch of the same stable_id,
 * or it races this self-schedule for the identical cooldown from the same
 * starting instant and double-fires (found reviewing this very fix: a
 * deck-load's own manual retry-once used to do exactly that).
 *
 * The scheduled callback checks `_hasActiveConsumer` at FIRE time, not at
 * schedule time: a consumer present now may be gone by the time the
 * cooldown elapses (row deselected, deck unloaded), and that is exactly
 * the case this guard exists for. A dropped retry does not erase the cache
 * entry or its now-past `retryAfter` - it stays the retryable class and
 * due for refetch, so a consumer returning later (`ensureAnlz` via
 * `_dueForEnsureRefetch`) revives it with no special-casing on either
 * side. */
function _publishAnlzResult(stable_id: string, data: AnlzData, alreadyScoped = false): void | Promise<void> {
	const existingTimer = _retryTimers.get(stable_id);
	if (existingTimer !== undefined) {
		clearTimeout(existingTimer);
		_retryTimers.delete(stable_id);
	}
	// `resolveDisplayedAnlz` below already prefers a cache-entry grid over the
	// deck's own, but that is only the DISPLAY projection: quantize, beat
	// loops, _synchronizeFollowers and the master-grid read all take the
	// engine's `deck.anlz`. A local deck that merged the synthetic fallback
	// grid, then had a vendor mapping land, gets its authoritative PQTZ grid
	// from exactly this ambient retry - and nothing was propagating it back,
	// so the waveform painted PQTZ while the beat math still ran on the
	// fallback (discussion_r3919779323 P1 BLOCKING). The sink is what closes
	// that gap; it fires for every real grid this cache learns about,
	// including the very first, and the engine decides whether any loaded deck
	// is actually holding a different one.
	// Passed through, never awaited: only refreshAnalysisSourceDecks awaits
	// this (discussion_r3970967293); _fetchAndPublish stays fire-and-forget.
	const sinkSettlement =
		_authoritativeGridSink !== null && hasAnlzBeatgrid(data) ? _authoritativeGridSink(stable_id, data, true, alreadyScoped) : undefined;
	if (!isRetryableAnlzData(data)) {
		_cache[stable_id] = { status: 'ready', data };
		return sinkSettlement;
	}
	_cache[stable_id] = { status: 'ready', data, retryAfter: performance.now() + RETRYABLE_COOLDOWN_MS };
	_retryTimers.set(
		stable_id,
		setTimeout(() => {
			_retryTimers.delete(stable_id);
			if (!_hasActiveConsumer(stable_id)) return;
			_fetchAndPublish(stable_id);
		}, RETRYABLE_COOLDOWN_MS)
	);
	return sinkSettlement;
}

/** Notified with every /anlz payload this cache learns that carries a REAL
 * beatgrid, so the engine can adopt it over a deck's merged fallback grid.
 * Installed rather than imported: this module is a display-layer cache and
 * must not reach into audio-engine.svelte.ts, which imports the other way
 * round already. Install is once-only; a second install is a wiring bug and
 * throws rather than silently replacing the first sink. */
export type AuthoritativeAnlzGridSink = (
	stable_id: string,
	data: AnlzData,
	/** Whether `data` carries a usable grid. Defaults true because the ambient
	 * retry above only ever notifies about a REAL grid; the rbx-vs-own switch
	 * in `refreshAnalysisSourceDecks` passes false when the newly selected
	 * source has no grid for this track, so the deck settles gridless instead
	 * of silently dropping Beat Sync. */
	landed?: boolean,
	/** True only from inside refreshAnalysisSourceDecks's own claim: reconcile
	 * INLINE, a fresh nested claim there deadlocks against it (r3972154599). */
	alreadyScoped?: boolean
) => void | Promise<void>;
let _authoritativeGridSink: AuthoritativeAnlzGridSink | null = null;

export function installAuthoritativeAnlzGridSink(sink: AuthoritativeAnlzGridSink): void {
	if (_authoritativeGridSink !== null) {
		throw new Error('an authoritative anlz grid sink is already installed');
	}
	_authoritativeGridSink = sink;
}

/** Notifies the installed sink (if any) that `stable_id` settled WITHOUT a
 * beatgrid, so a deck that was relying on one can be abandoned rather than
 * left silently scheduled against a grid that no longer exists. Exported so
 * `refreshAnalysisSourceDecks` (analysis-source-refresh.ts) can reach the
 * sink without touching `_authoritativeGridSink` directly - that variable
 * stays private to this module. A no-op before any sink is installed. */
export function notifyGridlessSettlement(
	stable_id: string,
	data: AnlzData,
	alreadyScoped = false
): void | Promise<void> {
	if (_authoritativeGridSink === null) return;
	return _authoritativeGridSink(stable_id, data, false, alreadyScoped);
}

/** True when a loaded deck's own `$effect` (WaveRow.svelte) should call
 * `ensureAnlz` again for it: no anlz published yet, or the anlz that IS
 * published is still the retryable class. A deck load's own
 * `fetchAnlzForDeckLoad` never blocks the load waiting one out and can
 * publish a retryable payload straight away if the decoder is saturated (up
 * to 180s of admission/slot saturation per the backend's own ceiling, far
 * longer than one 2s cooldown) - `deck.anlz !== null` alone must not read as "done" in
 * that case, or the deck stays blank with no further retry (Codex finding,
 * issue #735 follow-up, discussion_r3907741366). An `anlz_error` IS terminal
 * - it means the fetch itself failed or the backend gave an explicit
 * permanent answer, neither of which this retryable class covers. */
export function deckAnlzNeedsFetch(anlz: AnlzData | null, anlz_error: string | null): boolean {
	if (anlz_error !== null) return false;
	if (anlz === null) return true;
	return isRetryableAnlzData(anlz);
}

/** Resolves the anlz a loaded deck's row should RENDER: the deck's own
 * published anlz if it is terminal (unchanged fast path for every normal
 * load), else the freshest cache entry for its stable_id if one exists,
 * else whatever the deck currently holds. `ensureAnlz`'s ambient retry
 * (driven by `deckAnlzNeedsFetch`) writes its result to the shared cache,
 * NOT to the deck - without preferring the cache here, a deck published
 * with a still-retryable anlz would keep re-fetching forever yet never
 * actually show the decode once it lands (Codex finding, issue #735
 * follow-up, discussion_r3907741366 - the display half of the same gap).
 *
 * A real cache-entry beatgrid always wins over the deck's own: the cache's
 * ambient waveform retry hits the same `/anlz` endpoint, so once ITS payload
 * carries a real grid (e.g. a rekordbox mapping landed and the retry raced
 * ahead of the engine's own PARITY-10 upgrade fetch) that grid is at least as
 * fresh as whatever the deck already holds, and must not be discarded in
 * favor of a possibly-stale fallback one (discussion_r3916394792).
 *
 * Only when the cache entry does NOT yet have a real grid does the deck's own
 * beatgrid win: PARITY-10's deferred upgrade merges the analysis-derived grid
 * into `deck.anlz` without touching `local_waveform`, so a track whose
 * waveform decode is still retryable keeps reporting retryable here forever
 * even after its grid has landed. Preferring the cache entry WHOLESALE in
 * that case would silently drop the already-merged grid every waveform
 * refetch - quantize and Beat Sync read the engine's `deck.anlz`, so this
 * row's paint must never disagree with them about whether a grid exists. */
export function resolveDisplayedAnlz(
	deckAnlz: AnlzData | null,
	stable_id: string | null
): AnlzData | null {
	if (deckAnlz !== null && !isRetryableAnlzData(deckAnlz)) return deckAnlz;
	if (stable_id === null) return deckAnlz;
	const entry = _cache[stable_id];
	if (entry === undefined || entry.status !== 'ready') return deckAnlz;
	if (hasAnlzBeatgrid(entry.data)) return entry.data;
	if (deckAnlz !== null && hasAnlzBeatgrid(deckAnlz)) return { ...entry.data, beatgrid: deckAnlz.beatgrid };
	return entry.data;
}

/** Fetches /anlz for a deck load and publishes whatever it settles on into
 * the shared cache. A deck load needs a terminal-ish answer PROMPTLY to
 * publish into `st.anlz` - it cannot block the load out waiting for a
 * decoder to free up (audio-engine's "candidate deck transaction is
 * incomplete" invariant forbids leaving anlz null), so this returns the
 * FIRST fetch's result even if it is still the retryable class (Codex
 * finding, issue #735 follow-up, discussion_r3907610439).
 *
 * A retryable result is not abandoned there: `_publishAnlzResult`'s own
 * ambient self-schedule keeps retrying this stable_id every cooldown until
 * a terminal answer lands, and `deckAnlzNeedsFetch` + `resolveDisplayedAnlz`
 * pick that up for the deck's own render once it does (issue #735
 * follow-up, discussion_r3907741366). This function used to ALSO hand-roll
 * its own single delayed retry here - once the ambient self-schedule was
 * added, that second mechanism raced it for the exact same cooldown from
 * the exact same starting instant and double-fired the retry (Codex
 * finding, issue #735 follow-up, discussion_r3907928251 fix review); one
 * retry mechanism for a given stable_id, not two.
 *
 * Same generation guard as `_fetchAndPublish`, but a deck-load candidate
 * cannot merely discard the cache write: `load()` would still publish its
 * returned payload after audio decoding. Re-fetch until the result belongs to
 * the current generation, so a source switch during a load never publishes a
 * superseded grid onto that deck (discussion_r3923866060). Retries via
 * `fetchAnlzUntilCurrentGeneration` below. */
export async function fetchAnlzForDeckLoad(stable_id: string): Promise<AnlzData> {
	const data = await fetchAnlzUntilCurrentGeneration(() => fetchAnlz(stable_id));
	_publishAnlzResult(stable_id, data);
	return data;
}

/** Retries `fetch` until its answer belongs to the CURRENT anlz-fetch
 * generation, discarding any result that resolved after a mid-flight
 * analysis-source switch (PARITY-02) already bumped it and wiped the shared
 * cache (anlz-fetch-generation.ts) - publishing a stale answer over a
 * just-wiped entry would repopulate it with pre-switch bytes even though
 * `analysisSourceState` already recorded the new source, a permanent split
 * invisible to `_decksDisagreeWith`'s own-vs-own comparison
 * (discussion_r3973991964 P1 BLOCKING). Shared by `fetchAnlzForDeckLoad`
 * above and `RbAudioEngine.refreshHotCues` (audio-engine.svelte.ts), which
 * used to hand-roll this loop separately - the audio-engine copy was the one
 * missing the guard until the finding above. */
export async function fetchAnlzUntilCurrentGeneration<T>(fetch: () => Promise<T>): Promise<T> {
	for (;;) {
		const generation = currentAnlzFetchGeneration();
		const data = await fetch();
		if (generation !== currentAnlzFetchGeneration()) continue;
		return data;
	}
}

/** True only for a 'ready' entry that is safe to reuse as a cache HIT (deck
 * load, prefetch, etc). A retryable entry (transient decoder-saturation
 * reject, issue #735 follow-up) must NOT satisfy a cache check even once its
 * cooldown has elapsed: nothing outside `ensureAnlz` schedules the promised
 * retry, so a consumer that treated a stale retryable entry as a hit would
 * pin the empty payload to the deck indefinitely, past `retryAfter`, until
 * the user happens to reselect the row. Callers that get `false` here must
 * re-fetch, exactly like an uncached stable_id. */
export function isAnlzEntryUsable(entry: AnlzEntry | undefined): entry is Extract<AnlzEntry, { status: 'ready' }> {
	return entry !== undefined && entry.status === 'ready' && !_isRetryableEntry(entry);
}

/** Kick off the /anlz fetch for a track unless already cached/in-flight with
 * a terminal result. MUTATES rune state - call from $effect, never from
 * $derived.
 *
 * Timed on the MISS path only (PERF-R5 Q9): a cache hit returns above without
 * doing work, so including it would drag the sampled figure toward zero and
 * hide the cold fetch this warm-up exists to pay for. Sampled 1 in 5 because
 * arrow-key row selection fires this in bursts. */
export function ensureAnlz(stable_id: string): void {
	const existing = _cache[stable_id];
	if (existing !== undefined && !_dueForEnsureRefetch(existing)) return;
	_fetchAndPublish(stable_id);
}

/** Pure read; undefined = never requested for this stable_id. */
export function getAnlzEntry(stable_id: string): AnlzEntry | undefined {
	return _cache[stable_id];
}

/** Overwrites the shared cache entry with a known-fresh /anlz payload,
 * without triggering a fetch of its own. `refreshHotCues` (audio-engine)
 * calls this after a hot-cue save/clear/restore: it already fetches a fresh
 * `/anlz` for the deck's OWN state, but that fetch bypassed this module
 * (`fetchAnlz` direct, not `fetchAnlzForDeckLoad`/`ensureAnlz`), so the
 * SHARED cache entry stayed at its pre-mutation value. `load()`'s cache hit
 * (`isAnlzEntryUsable`) has no freshness check beyond "ready", so a later
 * reload of the same track - on this deck or another - would reuse that
 * stale entry: the hot cue bank (always a live fetch) would show the new
 * cue while the waveform (from the stale cached anlz) would not. Same
 * failure shape as issue #877's reported bug, just triggered by a reload
 * instead of the original paint defect. Returns the sink's settlement;
 * only `refreshAnalysisSourceDecks` below awaits it. */
export function refreshAnlzCacheEntry(
	stable_id: string,
	data: AnlzData,
	alreadyScoped = false
): void | Promise<void> {
	return _publishAnlzResult(stable_id, data, alreadyScoped);
}

/** Evicts a cache entry outright, so it reads back as never-requested
 * (`getAnlzEntry` -> undefined, `ensureAnlz` treats it as a miss). Used by
 * `refreshHotCues` (audio-engine) when one of its two post-write GETs fails:
 * `Promise.all` rejects before `refreshAnlzCacheEntry` runs, so without this
 * the pre-mutation entry stays 'ready' and `isAnlzEntryUsable` keeps serving
 * it to a later load() forever, reaching issue #877's same stale-waveform
 * shape from a failed-refresh path instead of the original paint defect
 * (discussion_r3918817422). */
export function invalidateAnlzCacheEntry(stable_id: string): void {
	delete _cache[stable_id];
}

/** Evicts every cached entry outright, so each reads back as never-requested
 * exactly like `invalidateAnlzCacheEntry`, all at once. Used when a change
 * invalidates a field embedded in EVERY track's /anlz payload rather than one
 * stable_id's - the PARITY-02 rbx-vs-own analysis source toggle changes which
 * beatgrid a fresh /anlz response carries for every track, not just whichever
 * ones happen to be cached at switch time, so a per-id invalidation loop at
 * the call site would miss any track not already cached and still serve it a
 * stale hit later (discussion_r3921666943). */
export function invalidateAllAnlzCacheEntries(): void {
	for (const stable_id of Object.keys(_cache)) delete _cache[stable_id];
}

/** Evicts every READY entry whose stamped `beatgrid_source` disagrees with
 * `wantedSource`, leaving one that already agrees (or is still loading/error)
 * untouched. Returns whether anything was evicted.
 *
 * `refreshAnalysisSourceDecks` only runs when a LOADED deck disagrees with
 * the new source (`_decksDisagreeWith`, analysis-source.svelte.ts) - a track
 * merely prefetched by library browsing (`ensureAnlz`, never loaded onto a
 * deck) is invisible to that check, so a switch with no loaded deck to
 * disagree left such an entry cached under the OLD source indefinitely. The
 * next deck that loads that track then gets an `isAnlzEntryUsable` cache HIT
 * on stale bytes even though the toggle already reports the new source
 * (discussion_r3973991969 P1 BLOCKING). */
export function evictAnlzCacheEntriesServingOtherSource(
	wantedSource: 'rekordbox' | 'own'
): boolean {
	let evictedAny = false;
	for (const [stable_id, entry] of Object.entries(_cache)) {
		if (entry.status === 'ready' && entry.data.beatgrid_source !== wantedSource) {
			delete _cache[stable_id];
			evictedAny = true;
		}
	}
	return evictedAny;
}

/** Public entry point for the PARITY-02 rbx-vs-own deck refresh. The actual
 * implementation lives in analysis-source-refresh.ts (kept out of this file
 * to stay under the repo's 600-line file-size ratchet); this wrapper supplies
 * that module's ports with this cache's own primitives so it never needs to
 * import this file back (see analysis-source-refresh.ts's own docstring for
 * why a reverse import would close a cycle). */
export function refreshAnalysisSourceDecks(
	deckIds: readonly _RefreshDeckId[],
	decks: Record<_RefreshDeckId, AnalysisSourceRefreshDeck>,
	isSuperseded?: () => boolean
): Promise<'rekordbox' | 'own' | null> {
	return _refreshAnalysisSourceDecksImpl(
		deckIds,
		decks,
		{
			invalidateAllAnlzCacheEntries,
			refreshAnlzCacheEntry,
			notifyGridlessSettlement,
			fetchAnlzBypassingHttpCache
		},
		isSuperseded
	);
}

/** Count of ready ANLZ entries for memory tracking. */
export function anlzCacheEntryCount(): number {
	let count = 0;
	for (const entry of Object.values(_cache)) {
		if (entry.status === 'ready') count++;
	}
	return count;
}

/** Estimated bytes of ready ANLZ JSON for hover breakdown only.
 * ~1.2 MB/track at points=38400. Cache grows on select/deck ensureAnlz,
 * not on table scroll. */
export function anlzCacheEstimatedBytes(): number {
	return anlzCacheEntryCount() * 1.2 * 1024 * 1024;
}
