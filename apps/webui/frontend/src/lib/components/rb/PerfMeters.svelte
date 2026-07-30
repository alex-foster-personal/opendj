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
	 */
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
	let memoryMB = $state(0);
	let memoryLevel: 'ok' | 'warn' | 'crit' = $state('ok');
	let memoryHover = $state('');

	function _updateMemory() {
		const jsHeapMB =
			typeof performance !== 'undefined' && 'memory' in performance
				? Math.round(
						((performance.memory as { usedJSHeapSize?: number }).usedJSHeapSize ?? 0) /
							(1024 * 1024)
					)
				: 0;
		const anlzMB = Math.round(anlzCacheEstimatedBytes() / (1024 * 1024));
		const anlzCount = anlzCacheEntryCount();
		const pcmMB = Math.round(deckPcmEstimatedBytes() / (1024 * 1024));
		const prefetchMB = Math.round(cacheBytes / (1024 * 1024));
		// ANLZ JSON + prefetch ArrayBuffers live in the JS heap. Decoded
		// AudioBuffers often do not - only add PCM on top of heap.
		const totalMB = jsHeapMB + pcmMB;

		memoryMB = totalMB;
		memoryLevel = totalMB > 1024 ? 'crit' : totalMB > 512 ? 'warn' : 'ok';
		memoryHover =
			`Approx retained: ${totalMB} MB (JS heap + decoded PCM)\n` +
			`• JS heap: ${jsHeapMB} MB\n` +
			`• ANLZ cache: ~${anlzMB} MB (${anlzCount} tracks, UNCAPPED, in heap)\n` +
			`• Deck PCM (+ 4 stems when ready): ${pcmMB} MB\n` +
			`• Audio prefetch: ${prefetchMB} MB (${cacheN} tracks, in heap)`;
	}

	// Sample every 2s
	let _interval: ReturnType<typeof setInterval> | null = null;
	$effect(() => {
		_updateMemory();
		_interval = setInterval(_updateMemory, 2000);
		return () => {
			if (_interval !== null) clearInterval(_interval);
		};
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
>{memoryMB}M</span>

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
