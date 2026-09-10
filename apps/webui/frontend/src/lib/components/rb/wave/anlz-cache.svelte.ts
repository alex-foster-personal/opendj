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
import { analysisSourceState } from '$lib/rb/analysis-source-state.svelte';
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
 * shared by `ensureAnlz` (gated behind `_dueForEnsureRefetch`) and the
 * ambient retry timer in `_publishAnlzResult`, which must NOT re-apply that
 * gate: it fires exactly at the cooldown it itself scheduled, so re-checking
 * `performance.now() >= retryAfter` against a second, separately read clock
 * sample can lose that comparison by a sub-millisecond hair and silently
 * kill the retry chain outright (Codex finding, issue #735 follow-up,
 * discussion_r3907928251 fix review).
 *
 * Captures the fetch generation at the moment the fetch is ISSUED, not when
 * it settles: a mid-flight switch (PARITY-02) wipes this cache via
 * `invalidateAllAnlzCacheEntries`, but an already-in-flight fetch under the
 * OLD generation has no way to cancel, and would otherwise resurrect a
 * pre-switch 'ready' entry right after the wipe (discussion_r3921839825
 * follow-up). A mismatch at settle time discards the write outright and
 * clears its own `loading` placeholder (`_discardSuperseded` below) rather
 * than leaving it stuck forever (discussion_r3975650980 P2 BLOCKING). */
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
 * reads back as never-requested instead of stuck forever. Only clears the
 * placeholder THIS call wrote - a newer fetch's later `loading` marker, or an
 * already-settled result, must survive untouched. `BrowserPanel`'s reactive
 * cache observer merely consumes a ready entry rather than restarting a
 * missing one, so a selected-but-unloaded row whose prefetch got superseded
 * would otherwise never refetch until reselected; restarting here while the
 * stable_id still has an active consumer closes that gap. */
function _discardSuperseded(stable_id: string, generation: number): void {
	const entry = _cache[stable_id];
	if (entry === undefined || entry.status !== 'loading' || entry.generation !== generation) return;
	delete _cache[stable_id];
	if (_hasActiveConsumer(stable_id)) _fetchAndPublish(stable_id);
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
/** True when `data`'s stamped source disagrees with the last CONFIRMED
 * rbx-vs-own selection (`analysisSourceState.features.beatgrid`). `undefined`
 * means no poll has confirmed one yet (cold mount) - nothing to disagree
 * with. An RBX->OWN->RBX round trip inside one poll interval leaves
 * `currentAnlzFetchGeneration()` unchanged to a straggler issued and settled
 * entirely within that window, so a generation guard alone is not enough
 * (discussion_r3975650988's class). Shared by `_publishAnlzResult`'s
 * cache-write guard below and `fetchAnlzUntilSourceConfirmed`'s retry, so a
 * direct-publication caller and the cache agree on one definition of "wrong
 * source" (discussion_r3978049099 P1 BLOCKING). */
function _disagreesWithConfirmedSource(data: AnlzData): boolean {
	const confirmed = analysisSourceState.features.beatgrid;
	return confirmed !== undefined && confirmed !== data.beatgrid_source;
}

/** Positive form of `_disagreesWithConfirmedSource`, exported for
 * `refreshHotCues` (audio-engine.svelte.ts)'s own fast-path check. */
export function anlzMatchesConfirmedSource(data: AnlzData): boolean {
	return !_disagreesWithConfirmedSource(data);
}

/**
 * `alreadyScoped` means "this payload's source is already trusted", not just
 * "reconcile inline". The one `true` caller, `refreshAnalysisSourceDecks`, is
 * itself about to advance `analysisSourceState.features.beatgrid` to match
 * `data.beatgrid_source` once it returns - comparing its own payload against
 * the not-yet-updated mirror would reject the very write that is about to
 * make it current. Every other caller can race a switch that already
 * confirmed a different selection and must be rejected via
 * `_disagreesWithConfirmedSource` above.
 */
function _publishAnlzResult(stable_id: string, data: AnlzData, alreadyScoped = false): void | Promise<void> {
	if (!alreadyScoped && _disagreesWithConfirmedSource(data)) {
		// Discard, do not restart: `analysisSourceState.features.beatgrid` stays
		// at the OLD value for the whole switch (advanced only once
		// `refreshAnalysisSourceDecks` itself returns), so restarting here the
		// way `_discardSuperseded` does for a generation mismatch would just
		// mismatch again against every straggler until the switch completes -
		// unlike a generation bump, which is finite, an in-flight switch has no
		// bound on how long this mismatch stays true. Evicting leaves the id
		// reading as never-requested; the next `ensureAnlz`/load re-fetches
		// under whatever source is confirmed by then.
		delete _cache[stable_id];
		return;
	}
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
 * `ensureAnlz` again for it: no anlz published yet, or the published anlz is
 * still the retryable class (decoder saturated, up to 180s per the backend's
 * own ceiling, far longer than one 2s cooldown) - `deck.anlz !== null` alone
 * must not read as "done" then, or the deck stays blank with no further
 * retry (Codex finding, issue #735 follow-up, discussion_r3907741366). An
 * `anlz_error` IS terminal: the fetch failed or the backend gave an explicit
 * permanent answer, neither covered by the retryable class. */
export function deckAnlzNeedsFetch(anlz: AnlzData | null, anlz_error: string | null): boolean {
	if (anlz_error !== null) return false;
	if (anlz === null) return true;
	return isRetryableAnlzData(anlz);
}

/** Resolves the anlz a loaded deck's row should RENDER: the deck's own
 * published anlz if terminal (unchanged fast path), else the freshest cache
 * entry for its stable_id if one exists, else whatever the deck holds. The
 * ambient retry (`deckAnlzNeedsFetch`) writes to the shared cache, NOT the
 * deck, so without preferring the cache here a still-retryable deck would
 * keep re-fetching forever yet never show the decode once it lands (issue
 * #735 follow-up, discussion_r3907741366).
 *
 * A real cache-entry beatgrid always wins over the deck's own: the cache's
 * ambient retry hits the same `/anlz` endpoint, so once ITS payload carries a
 * real grid it is at least as fresh as the deck's and must not be discarded
 * for a possibly-stale fallback (discussion_r3916394792). Only when the
 * cache entry has no real grid does the deck's own win: PARITY-10's deferred
 * upgrade merges the analysis-derived grid into `deck.anlz` without
 * touching `local_waveform`, so preferring the cache WHOLESALE there would
 * silently drop an already-merged grid - quantize and Beat Sync read
 * `deck.anlz`, so this row's paint must never disagree with them. */
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
 * decoder to free up, so this returns the FIRST fetch's result even if it is
 * still the retryable class (Codex finding, issue #735 follow-up,
 * discussion_r3907610439). A retryable result is not abandoned there:
 * `_publishAnlzResult`'s own ambient self-schedule keeps retrying this
 * stable_id every cooldown until a terminal answer lands (issue #735
 * follow-up, discussion_r3907741366) - this function must not ALSO hand-roll
 * a second delayed retry, or the two race the same cooldown and double-fire
 * (discussion_r3907928251 fix review).
 *
 * Retries via `fetchAnlzUntilSourceConfirmed` below rather than a plain
 * generation check: `load()` cannot merely discard a stale cache write the
 * way `_fetchAndPublish` does, since it still publishes its returned payload
 * after audio decoding regardless. */
export async function fetchAnlzForDeckLoad(stable_id: string): Promise<AnlzData> {
	const data = await fetchAnlzUntilSourceConfirmed(() => fetchAnlz(stable_id));
	_publishAnlzResult(stable_id, data);
	return data;
}

/** Retries `fetch` until its answer belongs to the CURRENT anlz-fetch
 * generation, discarding any result that resolved after a mid-flight
 * analysis-source switch (PARITY-02) already bumped it and wiped the shared
 * cache (anlz-fetch-generation.ts) - publishing a stale answer over a
 * just-wiped entry would repopulate it with pre-switch bytes even though
 * `analysisSourceState` already recorded the new source
 * (discussion_r3973991964 P1 BLOCKING). Shared by `fetchAnlzUntilSourceConfirmed`
 * below. */
export async function fetchAnlzUntilCurrentGeneration<T>(fetch: () => Promise<T>): Promise<T> {
	for (;;) {
		const generation = currentAnlzFetchGeneration();
		const data = await fetch();
		if (generation !== currentAnlzFetchGeneration()) continue;
		return data;
	}
}

/** Like `fetchAnlzUntilCurrentGeneration`, but also retries when the answer's
 * stamped source disagrees with the confirmed selection. The generation guard
 * alone only catches a switch THIS client itself drove
 * (`refreshAnalysisSourceDecks` bumps it) - an external client's direct PUT
 * to /api/v1/analysis/source never touches this client's generation counter,
 * so a straggling direct-publication fetch (`fetchAnlzForDeckLoad`,
 * `RbAudioEngine.refreshHotCues`) could settle stamped with a since-reverted
 * source with the generation guard seeing nothing wrong.
 * `_publishAnlzResult` already discards such a write from the shared cache,
 * but both callers used to return/install the rejected payload onto their
 * own deck regardless of that rejection (discussion_r3978049099 P1
 * BLOCKING). */
export async function fetchAnlzUntilSourceConfirmed(fetch: () => Promise<AnlzData>): Promise<AnlzData> {
	for (;;) {
		const data = await fetchAnlzUntilCurrentGeneration(fetch);
		if (anlzMatchesConfirmedSource(data)) return data;
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
 * calls this after a hot-cue save/clear/restore: its own fresh `/anlz` fetch
 * bypasses this module, so without this call the SHARED cache entry stays at
 * its pre-mutation value and a later reload elsewhere reuses stale bytes -
 * issue #877's failure shape, triggered by a reload. Returns the sink's
 * settlement; only `refreshAnalysisSourceDecks` below awaits it. */
export function refreshAnlzCacheEntry(
	stable_id: string,
	data: AnlzData,
	alreadyScoped = false
): void | Promise<void> {
	return _publishAnlzResult(stable_id, data, alreadyScoped);
}

/** Evicts a cache entry outright, so it reads back as never-requested. Used
 * by `refreshHotCues` when one of its two post-write GETs fails: without
 * this the pre-mutation entry stays 'ready' and keeps serving stale bytes to
 * a later load() forever (discussion_r3918817422). */
export function invalidateAnlzCacheEntry(stable_id: string): void {
	delete _cache[stable_id];
}

/** Evicts every cached entry outright, all at once. Used when a change
 * invalidates a field embedded in EVERY track's /anlz payload - the
 * PARITY-02 rbx-vs-own toggle changes which beatgrid a fresh response
 * carries for every track, so a per-id invalidation loop would miss any
 * track not already cached and still serve it a stale hit later
 * (discussion_r3921666943). */
export function invalidateAllAnlzCacheEntries(): void {
	for (const stable_id of Object.keys(_cache)) delete _cache[stable_id];
}

/** Evicts every READY entry whose stamped `beatgrid_source` disagrees with
 * `wantedSource`, leaving an agreeing (or still loading/error) entry
 * untouched. Returns whether anything was evicted. A track merely prefetched
 * by library browsing is invisible to `refreshAnalysisSourceDecks`'s own
 * loaded-deck check, so a switch with no loaded deck to disagree left such an
 * entry cached under the OLD source indefinitely (discussion_r3973991969 P1
 * BLOCKING). */
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
			fetchAnlzBypassingHttpCache,
			getReadyAnlz: (stable_id) => {
				const entry = getAnlzEntry(stable_id);
				return isAnlzEntryUsable(entry) ? entry.data : null;
			}
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
