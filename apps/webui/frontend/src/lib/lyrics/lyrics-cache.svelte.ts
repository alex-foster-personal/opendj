/**
 * On-demand lyric cache - the ONE place word/line payloads live in RAM.
 *
 * A playlist's worth of word timings is
 * too much to preload, so lyrics load on demand - either when a row scrolls
 * into view (the preview-strip reveal pattern) or on a 500ms debounced
 * hover - and then STAY cached until evicted or the playlist changes.
 * B9 (Tue 6 Oct 2026): at most LYRICS_CACHE_MAX_SETTLED settled entries, least
 * recently loaded evicted first. AutoPlay added one entry per load with no
 * eviction, so a long set held every track's words. Entries live raw in a
 * SvelteMap (reactive per key, never deep-proxied) and are replaced, never edited.
 * Every consumer (library tooltip, scrub overlay, waveform lanes, deck
 * line, stage) reads through this store so one track is fetched once, not
 * once per surface.
 *
 * Failure honesty: a fetch error is cached as state 'error' with the
 * message (renders as a real failure, retryable), and a 404 as state
 * 'none' (the honest "pipeline has no data yet"), so consumers never
 * re-fan-out requests for tracks known to have nothing.
 */

import { SvelteMap } from 'svelte/reactivity';

import { getTrackLyricsWords, type CoverageVerdict, type KaraokeTrack } from '$lib/api-karaoke';
import { fetchTrackLyrics } from '$lib/rb/api-rb';
import { karaokeTrackFromLineLyrics } from './line-lyrics-to-karaoke';

export type LyricEntryState = 'loading' | 'loaded' | 'none' | 'error';

export interface LyricEntry {
	state: LyricEntryState;
	track: KaraokeTrack | null;
	error: string | null;
}

export const HOVER_DEBOUNCE_MS = 500;

/** Keys are stable_ids; entries include in-flight loads so double-request
 * storms cannot happen while a fetch is pending. */
const _cache = new SvelteMap<string, LyricEntry>();
/** Settled entries (loaded, none, error) kept; the least recently loaded go first. */
export const LYRICS_CACHE_MAX_SETTLED = 16;
/** Recency for eviction, kept OUT of reactive state so a load inside an effect
 * never invalidates the effect that called it. */
const _touched = new Map<string, number>();
let _touchSeq = 0;
const _pending = new Map<string, Promise<void>>();
const _hoverTimers = new Map<string, ReturnType<typeof setTimeout>>();

function _drop(stableId: string): void {
	_cache.delete(stableId);
	_touched.delete(stableId);
}

/** Store an entry (replace, never edit) and evict the least recently loaded
 * settled entries beyond the cap. In-flight loads are never evicted. */
function _put(stableId: string, entry: LyricEntry): void {
	_cache.set(stableId, entry);
	if (!_touched.has(stableId)) _touched.set(stableId, ++_touchSeq);
	let settled = 0;
	for (const held of _cache.values()) if (held.state !== 'loading') settled += 1;
	while (settled > LYRICS_CACHE_MAX_SETTLED) {
		let victim: string | null = null;
		let oldest = Infinity;
		for (const [id, held] of _cache) {
			const touched = _touched.get(id) ?? 0;
			if (held.state !== 'loading' && id !== stableId && touched < oldest) {
				oldest = touched;
				victim = id;
			}
		}
		if (victim === null) break;
		_drop(victim);
		settled -= 1;
	}
}

function _failureText(exc: unknown): string {
	if (exc instanceof Error) return exc.message;
	return String(exc);
}

export function lyricEntry(stableId: string): LyricEntry | null {
	return _cache.get(stableId) ?? null;
}

/** Kick a load (idempotent). Always fetches WITH lines: every surface that
 * wants words within the session will want lines too, and one shape keeps
 * the cache from holding half-filled variants. */
export function loadLyrics(stableId: string): Promise<void> {
	const existing = _cache.get(stableId);
	_touched.set(stableId, ++_touchSeq);
	if (existing && existing.state !== 'error') {
		return _pending.get(stableId) ?? Promise.resolve();
	}
	_put(stableId, { state: 'loading', track: null, error: null });
	const run = getTrackLyricsWords(stableId, { includeLines: true })
		.then(async (track) => {
			if (track !== null && track.words.length > 0) {
				_put(stableId, { state: 'loaded', track, error: null });
				return;
			}
			let lineLyrics: Awaited<ReturnType<typeof fetchTrackLyrics>> = null;
			try {
				lineLyrics = await fetchTrackLyrics(stableId);
			} catch {
				lineLyrics = null;
			}
			if (lineLyrics !== null) {
				const synthesized = karaokeTrackFromLineLyrics(lineLyrics);
				if (synthesized !== null) {
					_put(stableId, { state: 'loaded', track: synthesized, error: null });
					return;
				}
			}
			_put(stableId, { state: 'none', track: null, error: null });
		})
		.catch((exc: unknown) => {
			_put(stableId, { state: 'error', track: null, error: _failureText(exc) });
		})
		.finally(() => {
			_pending.delete(stableId);
		});
	_pending.set(stableId, run);
	return run;
}

/** Debounced hover entry point (spec: 500ms so a scroll-past never costs a
 * request). Call cancelHoverLoad on pointer leave. */
export function hoverLoadLyrics(stableId: string): void {
	if (_cache.has(stableId) || _hoverTimers.has(stableId)) return;
	_hoverTimers.set(
		stableId,
		setTimeout(() => {
			_hoverTimers.delete(stableId);
			void loadLyrics(stableId);
		}, HOVER_DEBOUNCE_MS)
	);
}

export function cancelHoverLoad(stableId: string): void {
	const timer = _hoverTimers.get(stableId);
	if (timer !== undefined) {
		clearTimeout(timer);
		_hoverTimers.delete(stableId);
	}
}

/** Write-through hook for the override flow: after PUT
 * /lyrics/override succeeds, the caller pushes the server's verdict here so
 * every surface reading the cache (panel, deck, stage, tip) agrees at once
 * without a refetch. No-op unless the track is loaded. */
function applyVerdict(stableId: string, verdict: CoverageVerdict): void {
	const entry = _cache.get(stableId);
	if (entry !== undefined && entry.state === 'loaded' && entry.track !== null) {
		_put(stableId, {
			state: 'loaded',
			track: { ...entry.track, verdict },
			error: null
		});
	}
}

/** Playlist changed: drop everything except the ids still on screen (the
 * loaded decks, typically), per the RAM rule in the spec. */
function evictAllExcept(keep: Iterable<string> = []): void {
	const keepSet = new Set(keep);
	for (const key of [..._cache.keys()]) {
		if (!keepSet.has(key)) _drop(key);
	}
	for (const [key, timer] of _hoverTimers) {
		if (!keepSet.has(key)) {
			clearTimeout(timer);
			_hoverTimers.delete(key);
		}
	}
}

/** Test/diagnostic surface: how much is held (the config panel's number). */
function cacheStats(): { entries: number; loaded: number; words: number } {
	const entries = [..._cache.values()];
	return {
		entries: entries.length,
		loaded: entries.filter((e) => e.state === 'loaded').length,
		words: entries.reduce((n, e) => n + (e.track?.words.length ?? 0), 0)
	};
}

/**
 * The cache's whole surface, as ONE exported object.
 *
 * Deliberately a facade rather than seven loose named exports. This is a
 * module-level singleton, and three of its seven operations (hover debounce,
 * eviction, stats) land with PR-4a while the library and triage panels that
 * call them arrive in PR-4b - so as named exports they would read to the
 * dead-export ratchet, correctly, as four independent unreferenced decisions
 * rather than one cache with one lifecycle. Grouping them narrows the module
 * to the single thing it actually is, and `lyricsCache.load(id)` names what is
 * being touched at the call site.
 */
export const lyricsCache = {
	entry: lyricEntry,
	load: loadLyrics,
	hoverLoad: hoverLoadLyrics,
	cancelHover: cancelHoverLoad,
	applyVerdict,
	evictAllExcept,
	stats: cacheStats
} as const;
