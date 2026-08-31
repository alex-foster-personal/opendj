<script lang="ts">
	/**
	 * TopBar live performance meters (performance feature).
	 *
	 * Extracted from TopBar so health/cache chrome stays out of the mode
	 * dropdown / layout icon cluster. Readout only - does not alter audio.
	 *
	 * - Left number: presentation publish Hz (audio FPS analogue).
	 * - Middle number: ArrayBuffers in the row-select prefetch cache.
	 * - Right number: approx retained MB = JS heap + decoded PCM (often
	 *   outside the heap). ANLZ/prefetch are already inside the heap - do
	 *   not sum them again. Hover titles explain all (CLAUDE.md rule).
	 *   On a webview without performance.memory (WKWebView - the shipping
	 *   Tauri shell) the heap term does not exist, so the figure is decoded
	 *   PCM only, wears a '*' and carries its own threshold pair. The model
	 *   and the arithmetic behind those thresholds live in
	 *   $lib/rb/memory-meter-model (PERF-R5 Q10).
	 */
	import { untrack } from 'svelte';
	import {
		audioHealthHover,
		audioHealthHz,
		audioHealthLevel
	} from '$lib/rb/audio-health.svelte';
	import {
		audioPrefetchReadyBytes,
		audioPrefetchReadyCount,
		MAX_AUDIO_PREFETCH_BYTES,
		MAX_AUDIO_PREFETCH_TRACKS
	} from '$lib/rb/audio-prefetch-cache.svelte';
	import {
		anlzCacheEntryCount,
		anlzCacheEstimatedBytes
	} from '$lib/components/rb/wave/anlz-cache.svelte';
	import { deckPcmEstimatedBytes } from '$lib/rb/audio-engine.svelte';
	import { hasJsHeapApi, memoryReadout, readJsHeapMB } from '$lib/rb/memory-meter-model';

	const hz = $derived(audioHealthHz());
	const hzLevel = $derived(audioHealthLevel());
	const cacheN = $derived(audioPrefetchReadyCount());
	const cacheBytes = $derived(audioPrefetchReadyBytes());
	const cacheHover = $derived(
		`Prefetched audio tracks in memory: ${cacheN}/${MAX_AUDIO_PREFETCH_TRACKS} ` +
			`(${Math.round(cacheBytes / (1024 * 1024))} / ${MAX_AUDIO_PREFETCH_BYTES / (1024 * 1024)} MiB). ` +
			'LRU + byte budget - prevents RAM pressure that causes audible skips.'
	);

	// Memory tracking (sampled every 2s to avoid perf impact)
	let memoryText = $state('0M');
	let memoryLevel: 'ok' | 'warn' | 'crit' = $state('ok');
	let memoryHover = $state('');

	/** Feature-detected ONCE, not per sample. performance.memory is a
	 * Chromium-only API: it is either there for the whole session or never,
	 * and re-probing it every 2s invites treating "absent" as a transient 0. */
	const HEAP_API_PRESENT = typeof performance !== 'undefined' && hasJsHeapApi(performance);

	function _updateMemory(): void {
		const readout = memoryReadout({
			// null, not 0, when this webview cannot measure the heap - the
			// difference is what drives the marker and the threshold pair.
			jsHeapMB: HEAP_API_PRESENT ? readJsHeapMB(performance) : null,
			pcmMB: Math.round(deckPcmEstimatedBytes() / (1024 * 1024)),
			anlzMB: Math.round(anlzCacheEstimatedBytes() / (1024 * 1024)),
			anlzCount: anlzCacheEntryCount(),
			prefetchMB: Math.round(cacheBytes / (1024 * 1024)),
			prefetchCount: cacheN
		});
		memoryText = readout.text;
		memoryLevel = readout.level;
		memoryHover = readout.hover;
	}

	// Sample every 2s, and mean it.
	//
	// The previous version READ reactive state (cacheBytes, cacheN and the
	// rune-backed cache totals) inside the effect body, so every cache
	// mutation invalidated the effect, tore the interval down and started a
	// fresh one. The "every 2s" comment was false: the sampler re-armed on
	// each mutation and could sample far more often than it claimed. untrack
	// keeps the reads out of the dependency set, so the effect runs once on
	// mount and the interval genuinely owns the cadence.
	$effect(() => {
		untrack(_updateMemory);
		const timer = setInterval(() => untrack(_updateMemory), 2000);
		return () => clearInterval(timer);
	});
</script>

<span
	class="perf-meter"
	class:warn={hzLevel === 'warn'}
	class:crit={hzLevel === 'crit'}
	title={audioHealthHover()}
>{hz === null ? '--' : `${hz}`}</span>
<span class="perf-meter cache-n" title={cacheHover}>{cacheN}</span>
<span
	class="perf-meter memory-mb"
	class:warn={memoryLevel === 'warn'}
	class:crit={memoryLevel === 'crit'}
	title={memoryHover}
>{memoryText}</span>

<style>
	.perf-meter {
		font-size: 10px;
		font-variant-numeric: tabular-nums;
		letter-spacing: 0.02em;
		color: var(--rb-text-dim);
		min-width: 1.4em;
		text-align: right;
		user-select: none;
	}
	.perf-meter.cache-n {
		min-width: 0.8em;
		margin-right: 2px;
	}
	.perf-meter.memory-mb {
		min-width: 3em;
		margin-right: 2px;
	}
	.perf-meter.warn {
		color: #e89a3c;
	}
	.perf-meter.crit {
		color: #e05555;
	}
</style>
