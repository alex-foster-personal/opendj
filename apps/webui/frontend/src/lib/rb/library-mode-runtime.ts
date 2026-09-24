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

/** Called when /performance mounts so a later Library exit can tear down again. */
export function noteGigRuntimeMounted(): void {
	_releaseGeneration += 1;
	_releasedGeneration = null;
}

export async function releaseGigRuntime(opts?: {
	disposeEngine?: () => Promise<void>;
}): Promise<void> {
	const generation = _releaseGeneration;
	if (_releasedGeneration === generation) return;
	if (_releasePromise !== null) return _releasePromise;

	const disposeEngine = opts?.disposeEngine ?? (() => engine.dispose());

	_releasePromise = (async () => {
		clearAudioPrefetchCache();
		invalidateAllAnlzCacheEntries();
		await disposeStemDecoderPools();
		await disposeEngine();
		clearPerformanceSessionForLibraryExit();
		_releasedGeneration = generation;
		_publishLibraryModeIdle();
	})();

	try {
		await _releasePromise;
	} finally {
		_releasePromise = null;
	}
}

/** Test-only: allow repeated teardown assertions in one process. */
export function resetLibraryModeRuntimeForTest(): void {
	_releasePromise = null;
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
