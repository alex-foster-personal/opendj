<script lang="ts">
	/**
	 * TopBar live performance meters (performance feature).
	 *
	 * Extracted from TopBar so health/cache chrome stays out of the mode
	 * dropdown / layout icon cluster. Readout only - does not alter audio.
	 *
	 * - Left number: presentation publish Hz (audio FPS analogue).
	 * - Right number: ArrayBuffers in the row-select prefetch cache.
	 * Hover titles explain both (CLAUDE.md numeric-readout rule).
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

	const hz = $derived(audioHealthHz());
	const hzLevel = $derived(audioHealthLevel());
	const cacheN = $derived(audioPrefetchReadyCount());
	const cacheBytes = $derived(audioPrefetchReadyBytes());
	const cacheHover = $derived(
		`Prefetched audio tracks in memory: ${cacheN}/${MAX_AUDIO_PREFETCH_TRACKS} ` +
			`(${Math.round(cacheBytes / (1024 * 1024))} / ${MAX_AUDIO_PREFETCH_BYTES / (1024 * 1024)} MiB). ` +
			'LRU + byte budget - prevents RAM pressure that causes audible skips.'
	);
</script>

<span
	class="perf-meter"
	class:warn={hzLevel === 'warn'}
	class:crit={hzLevel === 'crit'}
	title={audioHealthHover()}
>{hz === null ? '--' : `${hz}`}</span>
<span class="perf-meter cache-n" title={cacheHover}>{cacheN}</span>

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
	.perf-meter.warn {
		color: #e89a3c;
	}
	.perf-meter.crit {
		color: #e05555;
	}
</style>
