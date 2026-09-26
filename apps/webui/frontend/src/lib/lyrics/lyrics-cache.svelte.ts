/**
 * On-demand lyric cache - the ONE place word/line payloads live in RAM.
 *
 * A playlist's worth of word timings is
 * too much to preload, so lyrics load on demand - either when a row scrolls
 * into view (the preview-strip reveal pattern) or on a 500ms debounced
 * hover - and then STAY cached until evicted or the playlist changes.
 * Every consumer (library tooltip, scrub overlay, waveform lanes, deck
 * line, stage) reads through this store so one track is fetched once, not
 * once per surface.
 *
 * Failure honesty: a fetch error is cached as state 'error' with the
 * message (renders as a real failure, retryable), and a 404 as state
 * 'none' (the honest "pipeline has no data yet"), so consumers never
 * re-fan-out requests for tracks known to have nothing.
 */

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
const _cache = $state<Record<string, LyricEntry>>({});
const _pending = new Map<string, Promise<void>>();
const _hoverTimers = new Map<string, ReturnType<typeof setTimeout>>();

function _failureText(exc: unknown): string {
	if (exc instanceof Error) return exc.message;
	return String(exc);
}

export function lyricEntry(stableId: string): LyricEntry | null {
	return _cache[stableId] ?? null;
}

/** Kick a load (idempotent). Always fetches WITH lines: every surface that
 * wants words within the session will want lines too, and one shape keeps
 * the cache from holding half-filled variants. */
export function loadLyrics(stableId: string): Promise<void> {
	const existing = _cache[stableId];
	if (existing && existing.state !== 'error') {
		return _pending.get(stableId) ?? Promise.resolve();
	}
	_cache[stableId] = { state: 'loading', track: null, error: null };
	const run = getTrackLyricsWords(stableId, { includeLines: true })
		.then(async (track) => {
			if (track !== null && track.words.length > 0) {
				_cache[stableId] = { state: 'loaded', track, error: null };
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
					_cache[stableId] = { state: 'loaded', track: synthesized, error: null };
					return;
				}
			}
			_cache[stableId] = { state: 'none', track: null, error: null };
		})
		.catch((exc: unknown) => {
			_cache[stableId] = { state: 'error', track: null, error: _failureText(exc) };
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
	if (_cache[stableId] || _hoverTimers.has(stableId)) return;
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
	const entry = _cache[stableId];
	if (entry !== undefined && entry.state === 'loaded' && entry.track !== null) {
		_cache[stableId] = {
			state: 'loaded',
			track: { ...entry.track, verdict },
			error: null
		};
	}
}

/** Playlist changed: drop everything except the ids still on screen (the
 * loaded decks, typically), per the RAM rule in the spec. */
function evictAllExcept(keep: Iterable<string> = []): void {
	const keepSet = new Set(keep);
	for (const key of Object.keys(_cache)) {
		if (!keepSet.has(key)) delete _cache[key];
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
	const entries = Object.values(_cache);
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
