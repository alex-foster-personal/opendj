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
