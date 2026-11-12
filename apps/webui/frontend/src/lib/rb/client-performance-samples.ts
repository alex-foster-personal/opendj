/**
 * Boot-deferred client performance samples for the engine telemetry sink.
 *
 * One JSONL copy per origin survives tab/port churn and feeds AGT-03 plus
 * GET /api/v1/errors when a deck or audio-health fault is present.
 */

import { releaseBeaconBody } from '$lib/api/release-body';
import { API_BASE } from '$lib/api/base';
import {
	anlzCacheEntryCount,
	anlzCacheEstimatedBytes
} from '$lib/components/rb/wave/anlz-cache.svelte';
import {
	audioPrefetchReadyBytes,
	audioPrefetchReadyCount
} from '$lib/rb/audio-prefetch-cache.svelte';
import { audioHealthHz, audioHealthLevel } from '$lib/rb/audio-health.svelte';
import { DECK_IDS, deckPcmEstimatedBytes, deckStates } from '$lib/rb/audio-engine.svelte';
import { bootScheduler, type BootScheduler } from './boot-scheduler';
import { readJsHeapMB } from '$lib/rb/js-heap-reading';
import { readPerfEvents } from '$lib/rb/perf-event-log';
import { frameProbeRequested } from '$lib/rb/frame-probe-flag';
import type { FrameProbe, FrameProbeWindow } from '$lib/rb/frame-probe';
import type { LiveHandlesTotals, LiveHandlesWindow } from '$lib/rb/live-handles-probe';
import type { DeckId } from '$lib/rb/deck-slots';

export const SAMPLE_INTERVAL_MS = 10_000;
const SAMPLE_PATH = '/api/v1/performance/telemetry/client-samples';

export type StemStatusWire = 'unavailable' | 'ready' | 'error';

export function mapStemStatus(status: string): StemStatusWire {
	if (status === 'ready') return 'ready';
	if (status === 'error') return 'error';
	return 'unavailable';
}

export interface DeckSampleSource {
	stable_id: string | null;
	duration_ms: number | null;
	playing: boolean;
	audible: boolean;
	transport_pending: boolean;
	stems: { status: string };
	last_load_latency_ms: number | null;
	sync_error: string | null;
	processor_error: string | null;
}

export function deckPerformanceSampleFromState(
	deckId: DeckId,
	deck: DeckSampleSource
) {
	return {
		deck_id: deckId,
		stable_id: deck.stable_id,
		duration_ms: deck.duration_ms,
		playing: deck.playing,
		audible: deck.audible,
		transport_pending: deck.transport_pending,
		stem_status: mapStemStatus(deck.stems.status),
		last_load_latency_ms: deck.last_load_latency_ms,
		sync_error: deck.sync_error,
		processor_error: deck.processor_error
	};
}

export function buildDeckSamples() {
	return DECK_IDS.map((deckId) => deckPerformanceSampleFromState(deckId, deckStates[deckId]));
}

export function buildClientPerformanceSamplePayload(
	clientSessionId: string,
	clientSampleId: string,
	frameProbe: FrameProbeWindow | null = null,
	liveHandles: LiveHandlesTotals | null = null
): Record<string, unknown> {
	return {
		client_sample_id: clientSampleId,
		client_session_id: clientSessionId,
		client_timestamp: new Date().toISOString(),
		route: window.location.pathname,
		page_uptime_ms: Math.max(0, performance.now()),
		js_heap_mb: readJsHeapMB(performance),
		pcm_estimated_mb: Math.round(deckPcmEstimatedBytes() / (1024 * 1024)),
		anlz_estimated_mb: Math.round(anlzCacheEstimatedBytes() / (1024 * 1024)),
		anlz_entry_count: anlzCacheEntryCount(),
		prefetch_mb: Math.round(audioPrefetchReadyBytes() / (1024 * 1024)),
		prefetch_count: audioPrefetchReadyCount(),
		audio_health_hz: audioHealthHz(),
		audio_health_level: audioHealthLevel(),
		perf_event_count: readPerfEvents().length,
		decks: buildDeckSamples(),
		frame_probe: frameProbe,
		// PERF-LEAK-11789: null unless ?perfprobe=1 armed the live handles probe.
		live_handles: liveHandles
	};
}

/** The four live handle totals (no by_site), or null when the probe is off. */
export function readLiveHandleTotals(): LiveHandlesTotals | null {
	try {
		const snap = (window as unknown as LiveHandlesWindow).__mdtLiveHandles?.();
		if (!snap) return null;
		const { intervals, long_timeouts, window_listeners, rafs } = snap;
		return { intervals, long_timeouts, window_listeners, rafs };
	} catch {
		return null;
	}
}

function storageOrNull(): Pick<Storage, 'getItem'> | null {
	try {
		return typeof localStorage === 'undefined' ? null : localStorage;
	} catch {
		return null;
	}
}

export function startClientPerformanceSampling(
	scheduler: BootScheduler = bootScheduler,
	startProbe?: () => FrameProbe
): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return () => {};
	}
	const clientSessionId = crypto.randomUUID();
	// PERFPROBE-01: opt-in only, read once at boot. The probe module loads
	// only when asked for, so it stays out of the first-load bundle.
	let stopped = false;
	let frameProbe: FrameProbe | null = null;
	if (frameProbeRequested(window.location.search ?? '', storageOrNull())) {
		if (startProbe) {
			frameProbe = startProbe();
		} else {
			void import('$lib/rb/frame-probe').then(
				(mod) => {
					if (!stopped) frameProbe = mod.startFrameProbe();
				},
				() => {}
			);
		}
	}
	let timer: ReturnType<typeof setInterval> | null = null;
	let inFlight = false;
	let firstScheduled = false;
	let firstReleased = false;

	const postSample = (): void => {
		if (inFlight || document.visibilityState !== 'visible') return;
		inFlight = true;
		const body = buildClientPerformanceSamplePayload(
			clientSessionId,
			crypto.randomUUID(),
			frameProbe?.take() ?? null,
			readLiveHandleTotals()
		);
		void fetch(`${API_BASE}${SAMPLE_PATH}`, {
			method: 'POST',
			keepalive: true,
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(body)
		})
			.then(releaseBeaconBody)
			.catch(() => {})
			.finally(() => {
				inFlight = false;
			});
	};

	const stopTimer = (): void => {
		if (timer === null) return;
		clearInterval(timer);
		timer = null;
	};

	const startTimer = (): void => {
		if (timer !== null) return;
		timer = setInterval(postSample, SAMPLE_INTERVAL_MS);
	};

	const runFirstSample = (): void => {
		firstReleased = true;
		if (document.visibilityState !== 'visible') return;
		postSample();
		startTimer();
	};

	const onVisibilityChange = (): void => {
		if (document.visibilityState === 'visible') {
			if (firstReleased) {
				postSample();
				startTimer();
			} else if (!firstScheduled) {
				firstScheduled = true;
				scheduler.defer('client-samples:first', runFirstSample);
			}
		} else {
			stopTimer();
		}
	};

	document.addEventListener('visibilitychange', onVisibilityChange);
	if (document.visibilityState === 'visible') {
		firstScheduled = true;
		scheduler.defer('client-samples:first', runFirstSample);
	}

	return () => {
		stopped = true;
		stopTimer();
		frameProbe?.stop();
		document.removeEventListener('visibilitychange', onVisibilityChange);
	};
}
