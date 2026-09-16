/**
 * Row-select audio ArrayBuffer prefetch (performance feature).
 *
 * Latency improvement (measured 2026-07-24, Proper Education ~8 MB MP3):
 * cold `fetchAudio` was 1052 ms of a 2289 ms load. A warm cache hit drops
 * that stage to ~0 ms so total lands near decode+stretch (~0.8-1.2 s).
 *
 * Select-path contract (95% no row-select latency):
 * - `ensureAudioPrefetch` never awaits; it only mutates intent + kicks a pump.
 * - Concurrency 1; newest select wins (AbortController cancels prior fetch).
 * - No decoded PCM here - ArrayBuffer only (PCM would be ~5-10x larger).
 *
 * RAM guard (CRITICAL - GC / memory pressure causes audible skips):
 * - Max {@link MAX_AUDIO_PREFETCH_TRACKS} entries AND {@link MAX_AUDIO_PREFETCH_BYTES}.
 * - LRU eviction on insert; failed/aborted entries do not retain bytes.
 * - `copyPrefetchedAudio` returns a slice so `decodeAudioData` cannot detach
 *   the cached buffer (browser may transfer the input ArrayBuffer).
 *
 * Affects: `BrowserPanel.selectRow`, `audio-engine.load` fetchAudio stage,
 * TrackTable cache markers, TopBar cache count. Does NOT change seek, anlz,
 * stretch create, or stem paths. Deferred alternatives (viewport lazy queue,
 * decoded-buffer LRU, HTTP audio cache) are documented on the perf issue.
 */
import { registerCapsConsumer } from '$lib/rb/cache-caps-registry';
import { audioUrl } from '$lib/rb/api-rb';
import { recordAudioPrefetchSampled } from '$lib/rb/library-perf';
import { prefetchByteCapForPosture, prefetchTrackCapForPosture, setResolvedPosture } from '$lib/rb/app-posture';
import { prefetchByteCap, prefetchTrackCap } from '$lib/rb/perf-tier';
import { pressureScaledPrefetchByteCap, pressureScaledPrefetchTrackCap } from '$lib/rb/prefetch-pressure-caps';

export { setResolvedPosture };

/** Soft cap on how many full files stay warm. Tier -> posture -> pressure (PERFMODE-04 Q29). */
export function MAX_AUDIO_PREFETCH_TRACKS(): number {
	return pressureScaledPrefetchTrackCap(prefetchTrackCapForPosture(prefetchTrackCap()));
}
/** Hard byte budget. Tier -> posture -> pressure (PERFMODE-04 Q29). */
export function MAX_AUDIO_PREFETCH_BYTES(): number {
	return pressureScaledPrefetchByteCap(prefetchByteCapForPosture(prefetchByteCap()));
}

/**
 * PERFMODE-04 shed bridge (audio-prefetch-cache-caps job). Null until
 * app-init.ts arms the background demand shed, following the same nullable
 * component-scope-bridge pattern as setSilenceDropoutHandler.
 */
let _shedRequest: ((id: 'audio-prefetch-cache-caps') => void) | null = null;

export function setAudioPrefetchShedRequest(fn: ((id: 'audio-prefetch-cache-caps') => void) | null): void {
	_shedRequest = fn;
}

/** Drain callback: kick the pump for whatever is currently wanted. */
export async function resumeAudioPrefetchOwedPump(): Promise<void> {
	void _pump();
}

export type AudioPrefetchStatus = 'loading' | 'ready' | 'error';

interface _ReadyEntry {
	status: 'ready';
	bytes: ArrayBuffer;
	bytesLength: number;
	touched: number;
}

interface _MetaEntry {
	status: 'loading' | 'error';
}

type _Entry = _ReadyEntry | _MetaEntry;

const _entries = $state<Record<string, _Entry>>({});
let _wanted: string | null = null;
let _busy = false;
let _controller: AbortController | null = null;
let _inflightSid: string | null = null;
let _touchSeq = 0;

function _readyBytesTotal(): number {
	let n = 0;
	for (const e of Object.values(_entries)) {
		if (e.status === 'ready') n += e.bytesLength;
	}
	return n;
}

function _readyCount(): number {
	let n = 0;
	for (const e of Object.values(_entries)) {
		if (e.status === 'ready') n += 1;
	}
	return n;
}

function _evictLruUntilFit(extraBytes: number): void {
	while (
		(_readyCount() >= MAX_AUDIO_PREFETCH_TRACKS() ||
			_readyBytesTotal() + extraBytes > MAX_AUDIO_PREFETCH_BYTES()) &&
		_readyCount() > 0
	) {
		let victim: string | null = null;
		let oldest = Infinity;
		for (const [sid, e] of Object.entries(_entries)) {
			if (e.status !== 'ready') continue;
			if (e.touched < oldest) {
				oldest = e.touched;
				victim = sid;
			}
		}
		if (victim === null) break;
		delete _entries[victim];
	}
}

function _insertReady(stable_id: string, bytes: ArrayBuffer): void {
	_evictLruUntilFit(bytes.byteLength);
	// Still over budget for a single huge file: keep it only if alone.
	if (bytes.byteLength > MAX_AUDIO_PREFETCH_BYTES()) {
		for (const sid of Object.keys(_entries)) {
			if (_entries[sid]?.status === 'ready') delete _entries[sid];
		}
	}
	_entries[stable_id] = {
		status: 'ready',
		bytes,
		bytesLength: bytes.byteLength,
		touched: ++_touchSeq
	};
}

async function _fetchBytes(stable_id: string, signal: AbortSignal): Promise<ArrayBuffer> {
	const r = await fetch(audioUrl(stable_id), { signal });
	if (!r.ok) {
		throw new Error(`audio prefetch HTTP ${r.status}`);
	}
	return r.arrayBuffer();
}

async function _pump(): Promise<void> {
	if (_busy) return;
	_busy = true;
	try {
		while (_wanted !== null) {
			const sid = _wanted;
			_wanted = null;
			const existing = _entries[sid];
			if (existing?.status === 'ready') continue;
			if (_controller !== null) {
				_controller.abort();
				_controller = null;
			}
			const ac = new AbortController();
			_controller = ac;
			_inflightSid = sid;
			_entries[sid] = { status: 'loading' };
			const startedAt = performance.now();
			try {
				const bytes = await _fetchBytes(sid, ac.signal);
				if (ac.signal.aborted) continue;
				_insertReady(sid, bytes);
				// Sampled 1 in 5 (PERF-R5 Q9): row selection fires this in
				// bursts, and an unsampled emitter would flush the 40-row perf
				// ring on one sweep down a playlist. Aborted and failed fetches
				// are NOT timed - neither one measures a completed warm.
				recordAudioPrefetchSampled(performance.now() - startedAt, bytes.byteLength);
			} catch (err: unknown) {
				const aborted =
					(err instanceof DOMException && err.name === 'AbortError') ||
					(err instanceof Error && err.name === 'AbortError');
				if (aborted) {
					if (_entries[sid]?.status === 'loading') delete _entries[sid];
					continue;
				}
				_entries[sid] = { status: 'error' };
			} finally {
				if (_inflightSid === sid) _inflightSid = null;
				if (_controller === ac) _controller = null;
			}
		}
	} finally {
		_busy = false;
		if (_wanted !== null) void _pump();
	}
}

/**
 * Kick a background audio fetch for `stable_id`. Safe on the select path:
 * never awaits. Newest intent wins; prior in-flight fetch is aborted.
 */
export function ensureAudioPrefetch(stable_id: string): void {
	if (stable_id.length === 0) return;
	const cur = _entries[stable_id];
	if (cur?.status === 'ready') {
		cur.touched = ++_touchSeq;
		return;
	}
	if (_inflightSid === stable_id && cur?.status === 'loading') return;
	_wanted = stable_id;
	if (cur === undefined) _entries[stable_id] = { status: 'loading' };
	// Intent is recorded above unconditionally (harmless, instant); only the
	// fetch pump itself is gated, so a shed under pressure never loses which
	// track was wanted, it just starts fetching it later.
	if (_shedRequest !== null) _shedRequest('audio-prefetch-cache-caps');
	else void _pump();
}

/** Reactive status for row markers; undefined = never requested. */
export function audioPrefetchStatus(stable_id: string): AudioPrefetchStatus | undefined {
	return _entries[stable_id]?.status;
}

/**
 * Copy of cached bytes for decode, or null on miss.
 * Copy avoids decodeAudioData detaching the cached buffer.
 */
export function copyPrefetchedAudio(stable_id: string): ArrayBuffer | null {
	const e = _entries[stable_id];
	if (e === undefined || e.status !== 'ready') return null;
	e.touched = ++_touchSeq;
	return e.bytes.slice(0);
}

/**
 * Decoded audio another player already holds, so a deck load can skip both
 * the fetch and the decode (decode is 54-72% of a cold load, see the CUEOUT-15
 * register row). The library preview provides it; audio-engine cannot import
 * preview-cue.svelte.ts directly without a cycle, so it meets it here, beside
 * the byte cache it already reads.
 *
 * Sharing is safe because nothing writes into a decoded buffer: the stretch
 * loader copies channels out with copyFromChannel, and the preview's cache
 * dropping its reference later cannot free a buffer the deck still holds.
 */
let _decodedAudioSource: ((stable_id: string) => AudioBuffer | null) | null = null;

export function provideDecodedAudio(source: (stable_id: string) => AudioBuffer | null): void {
	_decodedAudioSource = source;
}

/** A shared decoded buffer at `sampleRate`, or null. A rate mismatch is a miss. */
export function sharedDecodedAudio(stable_id: string, sampleRate: number): AudioBuffer | null {
	const buffer = _decodedAudioSource?.(stable_id) ?? null;
	return buffer !== null && buffer.sampleRate === sampleRate ? buffer : null;
}

/** Where a deck load's audio comes from, cheapest first. */
export interface DeckLoadAudio {
	shared: AudioBuffer | null;
	bytes: Promise<ArrayBuffer | null>;
	fetchStage: 'decodedShareHit' | 'fetchAudioCacheHit' | 'fetchAudio';
	decodeStage: 'decodeMixShared' | 'decodeMix';
	fetch: (stable_id: string) => Promise<ArrayBuffer>;
	stats: (bytes: ArrayBuffer | null) => Record<string, number>;
}

/**
 * Pick a deck load's audio source: the preview's decode (skips fetch and
 * decode), else prefetched bytes (skips fetch), else the network. Lives here
 * rather than in audio-engine, which is at its file-size allowance.
 * `sampleRate` is null while no AudioContext exists, which is always a miss.
 */
export function deckLoadAudio(
	stable_id: string,
	sampleRate: number | null,
	fetch: (stable_id: string) => Promise<ArrayBuffer>
): DeckLoadAudio {
	const shared = sampleRate === null ? null : sharedDecodedAudio(stable_id, sampleRate);
	// Prefetch hit: copyPrefetchedAudio (slice) so decode cannot detach cache.
	const prefetched = shared === null ? copyPrefetchedAudio(stable_id) : null;
	const fetchStage =
		shared !== null ? 'decodedShareHit' : prefetched !== null ? 'fetchAudioCacheHit' : 'fetchAudio';
	return {
		shared,
		bytes:
			shared !== null
				? Promise.resolve(null)
				: prefetched !== null
					? Promise.resolve(prefetched)
					: fetch(stable_id),
		fetchStage,
		decodeStage: shared !== null ? 'decodeMixShared' : 'decodeMix',
		fetch,
		stats: (bytes) => ({
			audioBytes: bytes?.byteLength ?? 0,
			audioPrefetchHit: prefetched !== null ? 1 : 0,
			decodedShareHit: shared !== null ? 1 : 0
		})
	};
}

/** Decode for a deck load, reusing the shared buffer when the context matches.
 * A context rebuilt at another rate since `deckLoadAudio` falls back to a fetch. */
export async function decodeDeckLoadAudio(
	ctx: BaseAudioContext,
	stable_id: string,
	audio: DeckLoadAudio,
	bytes: ArrayBuffer | null
): Promise<AudioBuffer> {
	if (audio.shared !== null && audio.shared.sampleRate === ctx.sampleRate) return audio.shared;
	return ctx.decodeAudioData(bytes ?? (await audio.fetch(stable_id)));
}

/** Ready track count for TopBar (hover explains caps). */
export function audioPrefetchReadyCount(): number {
	return _readyCount();
}

/** Ready bytes total for TopBar hover detail. */
export function audioPrefetchReadyBytes(): number {
	return _readyBytesTotal();
}

/** Re-run LRU eviction after a tier cap change. */
export function applyPrefetchCaps(): void {
	_evictLruUntilFit(0);
}
registerCapsConsumer('audio-prefetch', applyPrefetchCaps);

/**
 * Clear every prefetch entry and abort any in-flight fetch.
 * Used by the PerfMeters monitor reset (PERFMODE-05).
 */
export function clearAudioPrefetchCache(): void {
	if (_controller !== null) {
		_controller.abort();
		_controller = null;
	}
	_wanted = null;
	_inflightSid = null;
	for (const sid of Object.keys(_entries)) {
		delete _entries[sid];
	}
}
