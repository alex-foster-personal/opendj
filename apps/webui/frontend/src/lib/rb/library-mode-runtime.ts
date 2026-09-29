/**
 * PERFMODE-14: tear down Gig runtime when entering Library mode.
 */

import { anlzCacheEntryCount, invalidateAllAnlzCacheEntries } from '$lib/components/rb/wave/anlz-cache.svelte';
import {
	activeStemWorkerCount,
	disposeStemDecoderPools,
	pooledCount
} from '$lib/player/decode/flac-decoder-pool';
import { countRegisteredAudioContexts } from '$lib/rb/audio-context-registry';
import { audioContextState, engine, gigDeckGraphIsPresent } from '$lib/rb/audio-engine.svelte';
import { audioPrefetchReadyCount, clearAudioPrefetchCache } from '$lib/rb/audio-prefetch-cache.svelte';
import { PERFORMANCE_SESSION_STORAGE_KEY } from '$lib/rb/performance-session-snapshot';

const LIBRARY_MODE_EXIT_KEY = 'mdt.library_mode_exit';

let _releasePromise: Promise<void> | null = null;
let _releasePromiseGeneration: number | null = null;
let _releaseGeneration = 0;
let _releasedGeneration: number | null = null;

export interface LibraryModeIdleProbe {
	audio_context_state: AudioContextState | 'uninitialized';
	audio_context_count: number;
	prefetch_ready_count: number;
	anlz_cache_entry_count: number;
	stem_decoder_pooled_count: number;
	deck_nodes_present: boolean;
}

declare global {
	interface Window {
		__mdtLibraryModeIdle?: boolean;
		musicDjToolsLibraryMode?: {
			read_idle_probe(): LibraryModeIdleProbe;
		};
	}
}

export function readLibraryModeIdleProbe(): LibraryModeIdleProbe {
	const contextState = audioContextState();
	return {
		audio_context_state: contextState,
		audio_context_count: countRegisteredAudioContexts(),
		prefetch_ready_count: audioPrefetchReadyCount(),
		anlz_cache_entry_count: anlzCacheEntryCount(),
		stem_decoder_pooled_count: pooledCount() + activeStemWorkerCount(),
		deck_nodes_present: gigDeckGraphIsPresent()
	};
}

export function markLibraryModeExit(): void {
	if (typeof sessionStorage === 'undefined') return;
	sessionStorage.setItem(LIBRARY_MODE_EXIT_KEY, '1');
}

export function shouldSkipPerformanceSessionRestore(): boolean {
	if (typeof sessionStorage === 'undefined') return false;
	return sessionStorage.getItem(LIBRARY_MODE_EXIT_KEY) === '1';
}

export function consumeLibraryModeExitFlag(): void {
	if (typeof sessionStorage === 'undefined') return;
	sessionStorage.removeItem(LIBRARY_MODE_EXIT_KEY);
}

export function clearPerformanceSessionForLibraryExit(storage?: Storage): void {
	if (!shouldSkipPerformanceSessionRestore()) return;
	const sessionStorageRef = storage ?? (typeof localStorage !== 'undefined' ? localStorage : null);
	if (sessionStorageRef !== null) {
		sessionStorageRef.removeItem(PERFORMANCE_SESSION_STORAGE_KEY);
	}
}

function _publishLibraryModeIdle(): void {
	if (typeof window === 'undefined') return;
	window.__mdtLibraryModeIdle = true;
	const api = {
		read_idle_probe: () => readLibraryModeIdleProbe()
	};
	window.musicDjToolsLibraryMode = api;
}

/**
 * Called when /performance (Gig) OR Trackify mounts, so a later teardown of
 * whichever session mounted BEFORE it can detect it has been superseded and
 * skip disposing an engine the new mount has since claimed. Returns the new
 * generation so the caller can capture its own and recheck it later (Sol
 * review, PR #3676: the reverse Trackify-to-Gig teardown race needed the
 * same ownership check `releaseGigRuntime` already does for Gig-to-Trackify).
 */
export function noteGigRuntimeMounted(): number {
	_releaseGeneration += 1;
	_releasedGeneration = null;
	return _releaseGeneration;
}

/** The engine-ownership generation as of right now: compare a value captured
 * from `noteGigRuntimeMounted()`'s return against this immediately before an
 * unconditional `engine.dispose()` outside of `releaseGigRuntime` itself. */
export function currentGigRuntimeGeneration(): number {
	return _releaseGeneration;
}

export async function releaseGigRuntime(opts?: {
	disposeEngine?: () => Promise<void>;
}): Promise<void> {
	const generation = _releaseGeneration;
	if (_releasedGeneration === generation) return;
	// Only join an in-flight release for THIS SAME generation. A newer
	// generation (Gig remounted since the in-flight release started) must
	// start its own release rather than await a stale one that may already
	// be past the point of no return for disposing this generation's engine.
	if (_releasePromise !== null && _releasePromiseGeneration === generation) return _releasePromise;

	const disposeEngine = opts?.disposeEngine ?? (() => engine.dispose());

	// `ownPromise` (not the shared `_releasePromise`) is what THIS call awaits
	// and compares against in `finally`, so a concurrent newer-generation
	// call that replaces `_releasePromise` with its own promise can't have
	// its promise nulled out by this call's cleanup.
	const ownPromise: Promise<void> = (async () => {
		clearAudioPrefetchCache();
		invalidateAllAnlzCacheEntries();
		await disposeStemDecoderPools();
		// Re-check after every await: if Gig remounted (noteGigRuntimeMounted
		// bumped _releaseGeneration) while this stale release was still
		// in-flight, abort before disposing the singleton engine a live,
		// remounted Gig is now using, and before publishing idle=true over it.
		if (_releaseGeneration !== generation) return;
		await disposeEngine();
		if (_releaseGeneration !== generation) return;
		clearPerformanceSessionForLibraryExit();
		_releasedGeneration = generation;
		_publishLibraryModeIdle();
	})();
	_releasePromise = ownPromise;
	_releasePromiseGeneration = generation;

	try {
		await ownPromise;
	} finally {
		if (_releasePromise === ownPromise) {
			_releasePromise = null;
			_releasePromiseGeneration = null;
		}
	}
}

/** Test-only: allow repeated teardown assertions in one process. */
export function resetLibraryModeRuntimeForTest(): void {
	_releasePromise = null;
	_releasePromiseGeneration = null;
	_releaseGeneration = 0;
	_releasedGeneration = null;
	if (typeof window !== 'undefined') {
		window.__mdtLibraryModeIdle = false;
		delete window.musicDjToolsLibraryMode;
	}
	if (typeof sessionStorage !== 'undefined') {
		sessionStorage.removeItem(LIBRARY_MODE_EXIT_KEY);
	}
}
