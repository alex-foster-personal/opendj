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
	 *   not sum them again. The readouts carry no per-item title: one shared
	 *   card (PERF-UI-10, $lib/rb/perf-readout-card) explains all of them
	 *   (CLAUDE.md rule), opens on hover and pins on click.
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
		audioHealthLevel,
		audioHealthQuality,
		waveformStutterHover,
		waveformStutterSnapshot
	} from '$lib/rb/audio-health.svelte';
	import {
		audioPrefetchReadyBytes,
		audioPrefetchReadyCount,
		clearAudioPrefetchCache,
		MAX_AUDIO_PREFETCH_BYTES,
		MAX_AUDIO_PREFETCH_TRACKS
	} from '$lib/rb/audio-prefetch-cache.svelte';
	import {
		anlzEntryCap,
		prefetchTrackCap,
		tierHoverSuffix,
		tierLabelForHover
	} from '$lib/rb/perf-tier';
	import {
		anlzCacheEntryCount,
		anlzCacheEstimatedBytes,
		invalidateAllAnlzCacheEntries
	} from '$lib/components/rb/wave/anlz-cache.svelte';
	import { deckPcmEstimatedBytes } from '$lib/rb/audio-engine.svelte';
	import { hasJsHeapApi, memoryReadout, readJsHeapMB } from '$lib/rb/memory-meter-model';
	import { readMachinePressure } from '$lib/rb/machine-pressure';
	import {
		fetchProcessFamilySnapshot,
		readProcessFamilySnapshot
	} from '$lib/rb/process-family-snapshot';
	import {
		breakdownLines,
		type ChartSample,
		perfMeterSampleIntervalMs,
		pushSample
	} from '$lib/rb/perf-meter-model';
	import { readPerfEvents, recordPerfEvent, resetPerfEventLog } from '$lib/rb/perf-event-log';
	import {
		isPerfCardOpen,
		nextPerfCardState,
		PERF_CARD_CLOSED,
		perfReadoutRows,
		type PerfCardEvent,
		type PerfCardState
	} from '$lib/rb/perf-readout-card';
	import PerfMeterSpark from './PerfMeterSpark.svelte';

	const CHART_CAP = 60;

	const hz = $derived(audioHealthHz());
	const hzLevel = $derived(audioHealthLevel());
	const hzQuality = $derived(audioHealthQuality());
	const waveformStutter = $derived(waveformStutterSnapshot());
	const waveformStutterWarn = $derived(waveformStutter.stutters > 0);
	const cacheN = $derived(audioPrefetchReadyCount());
	const cacheBytes = $derived(audioPrefetchReadyBytes());
	const cacheHover = $derived(
		`Prefetched audio tracks in memory: ${cacheN}/${MAX_AUDIO_PREFETCH_TRACKS()} ` +
			`(${Math.round(cacheBytes / (1024 * 1024))} / ${MAX_AUDIO_PREFETCH_BYTES() / (1024 * 1024)} MiB). ` +
			`LRU + byte budget - prevents RAM pressure that causes audible skips. ${tierHoverSuffix()}`
	);

	// PERF-UI-10: one card for every compact readout. Hover opens it; a click
	// pins it. monitorOpen is the pin, and also gates the heavier sampler and
	// the chart / breakdown section below the readout rows.
	let cardState = $state<PerfCardState>(PERF_CARD_CLOSED);
	let monitorOpen = $state(false);
	let rootEl: HTMLDivElement | null = $state(null);
	const cardOpen = $derived(isPerfCardOpen(cardState));
	let memoryText = $state('0M');
	let memoryLevel: 'ok' | 'warn' | 'crit' = $state('ok');
	let memoryHover = $state('');
	let chartHistory = $state<ChartSample[]>([]);
	let hzDriftLogged = $state(false);

	const HEAP_API_PRESENT = typeof performance !== 'undefined' && hasJsHeapApi(performance);

	const hzDisplayLevel = $derived(
		!hzQuality.ok ? 'crit' : hzLevel === 'warn' ? 'warn' : hzLevel === 'crit' ? 'crit' : 'ok'
	);

	const breakdown = $derived(
		breakdownLines({
			hz,
			hzQualityOk: hzQuality.ok,
			hzTickHz: hzQuality.tickHz,
			prefetchCount: cacheN,
			prefetchMB: Math.round(cacheBytes / (1024 * 1024)),
			anlzCount: anlzCacheEntryCount(),
			anlzMB: Math.round(anlzCacheEstimatedBytes() / (1024 * 1024)),
			pcmMB: Math.round(deckPcmEstimatedBytes() / (1024 * 1024)),
			jsHeapMB: HEAP_API_PRESENT ? readJsHeapMB(performance) : null,
			ringCount: readPerfEvents().length,
			swapMB: readMachinePressure()?.swapUsedMb ?? null,
			kernelLevel: readProcessFamilySnapshot()?.kernelLevel ?? null,
			churnScore: readProcessFamilySnapshot()?.churnScore ?? null,
			compressorRate: readProcessFamilySnapshot()?.compressorRate ?? null,
			processes: readProcessFamilySnapshot()
		})
	);

	const hzText = $derived(hz === null ? '--' : `${hz}`);
	const waveformText = $derived(waveformStutter.active ? `W${waveformStutter.stutters}` : 'W--');
	const readoutRows = $derived(
		perfReadoutRows({
			hzText,
			hzDetail: audioHealthHover(),
			cacheText: `${cacheN}`,
			cacheDetail: cacheHover,
			waveformText,
			waveformDetail: waveformStutterHover(),
			memoryText,
			memoryDetail: memoryHover
		})
	);

	const hzChart = $derived(chartHistory.map((s) => s.hz));
	const cacheChart = $derived(chartHistory.map((s) => s.cacheMB));
	const memoryChart = $derived(chartHistory.map((s) => s.memoryMB));

	function _updateMemory(): void {
		const readout = memoryReadout({
			jsHeapMB: HEAP_API_PRESENT ? readJsHeapMB(performance) : null,
			pcmMB: Math.round(deckPcmEstimatedBytes() / (1024 * 1024)),
			anlzMB: Math.round(anlzCacheEstimatedBytes() / (1024 * 1024)),
			anlzCount: anlzCacheEntryCount(),
			prefetchMB: Math.round(cacheBytes / (1024 * 1024)),
			prefetchCount: cacheN,
			anlzCap: anlzEntryCap(),
			tierLabel: tierLabelForHover()
		});
		if (
			readout.text === memoryText &&
			readout.level === memoryLevel &&
			readout.hover === memoryHover
		) {
			return;
		}
		memoryText = readout.text;
		memoryLevel = readout.level;
		memoryHover = readout.hover;
	}

	function _pushChartSample(): void {
		const pressure = readMachinePressure();
		const processes = readProcessFamilySnapshot();
		const jsHeap = HEAP_API_PRESENT ? readJsHeapMB(performance) : null;
		const pcmMB = Math.round(deckPcmEstimatedBytes() / (1024 * 1024));
		const totalMB = (jsHeap ?? 0) + pcmMB;
		const point: ChartSample = {
			tMs: Date.now(),
			hz: audioHealthHz(),
			cacheN: audioPrefetchReadyCount(),
			cacheMB: Math.round(audioPrefetchReadyBytes() / (1024 * 1024)),
			memoryMB: totalMB,
			anlzMB: Math.round(anlzCacheEstimatedBytes() / (1024 * 1024)),
			prefetchMB: Math.round(audioPrefetchReadyBytes() / (1024 * 1024)),
			swapMB: pressure?.swapUsedMb ?? null,
			kernelLevel: processes?.kernelLevel ?? null,
			churnScore: processes?.churnScore ?? null
		};
		chartHistory = pushSample(chartHistory, point, CHART_CAP);
	}

	function _sampleTick(): void {
		untrack(_updateMemory);
		untrack(_pushChartSample);
	}

	function _kernelAndChurn(): { kernelLevel: number | null; churnScore: number | null } {
		const processes = readProcessFamilySnapshot();
		return {
			kernelLevel: processes?.kernelLevel ?? null,
			churnScore: processes?.churnScore ?? null
		};
	}

	function _cardEvent(event: PerfCardEvent): void {
		cardState = nextPerfCardState(cardState, event);
		monitorOpen = cardState.pinned;
	}

	function _onWindowPointerDown(event: PointerEvent): void {
		if (!cardState.pinned || rootEl === null) return;
		if (event.target instanceof Node && rootEl.contains(event.target)) return;
		_cardEvent('outside');
	}

	function _onWindowKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || !cardOpen) return;
		_cardEvent('escape');
	}

	function _handleReset(reset: 'prefetch' | 'anlz' | 'perf-ring'): void {
		if (reset === 'prefetch') clearAudioPrefetchCache();
		else if (reset === 'anlz') invalidateAllAnlzCacheEntries();
		else if (reset === 'perf-ring') resetPerfEventLog();
		_sampleTick();
	}

	$effect(() => {
		if (!hzQuality.ok && !hzDriftLogged) {
			hzDriftLogged = true;
			recordPerfEvent(
				'hz-meter-drift',
				`meter=${hzQuality.meterHz} tick=${hzQuality.tickHz?.toFixed(1)} error=${hzQuality.absError?.toFixed(1)}`,
				null,
				'error'
			);
		}
		if (hzQuality.ok) {
			hzDriftLogged = false;
		}
	});

	$effect(() => {
		if (!monitorOpen || typeof document === 'undefined') return;
		let intervalMs = perfMeterSampleIntervalMs(_kernelAndChurn());
		let timer: ReturnType<typeof setInterval> | null = null;

		const start = (): void => {
			if (timer !== null) return;
			_sampleTick();
			void fetchProcessFamilySnapshot();
			timer = setInterval(() => {
				untrack(_sampleTick);
				void fetchProcessFamilySnapshot();
			}, intervalMs);
		};

		const stop = (): void => {
			if (timer === null) return;
			clearInterval(timer);
			timer = null;
		};

		const reschedule = (): void => {
			const nextMs = perfMeterSampleIntervalMs(_kernelAndChurn());
			if (nextMs === intervalMs) return;
			intervalMs = nextMs;
			stop();
			if (document.visibilityState === 'visible') start();
		};

		const onVisibility = (): void => {
			if (document.visibilityState === 'visible') {
				start();
			} else {
				stop();
			}
		};

		if (document.visibilityState === 'visible') start();
		document.addEventListener('visibilitychange', onVisibility);
		const checkInterval = setInterval(reschedule, intervalMs);

		return () => {
			stop();
			clearInterval(checkInterval);
			document.removeEventListener('visibilitychange', onVisibility);
		};
	});

	$effect(() => {
		if (monitorOpen) return;
		untrack(_updateMemory);
		let intervalMs = perfMeterSampleIntervalMs(_kernelAndChurn());
		let timer: ReturnType<typeof setInterval> | null = null;

		const start = (): void => {
			if (timer !== null) return;
			untrack(_updateMemory);
			timer = setInterval(() => untrack(_updateMemory), intervalMs);
		};

		const stop = (): void => {
			if (timer === null) return;
			clearInterval(timer);
			timer = null;
		};

		const onVisibility = (): void => {
			if (document.visibilityState === 'visible') start();
			else stop();
		};

		if (typeof document !== 'undefined') {
			if (document.visibilityState === 'visible') start();
			document.addEventListener('visibilitychange', onVisibility);
		}

		return () => {
			stop();
			if (typeof document !== 'undefined') {
				document.removeEventListener('visibilitychange', onVisibility);
			}
		};
	});
</script>

<svelte:window onpointerdown={_onWindowPointerDown} onkeydown={_onWindowKeydown} />

<!-- PERF-UI-10: the compact readouts share ONE container and ONE card. No
     readout carries its own title, so neither the single-hover layer nor
     WKWebView draws a per-item tooltip; data-custom-tip marks the container
     as drawing its own rich hover. -->
<div
	class="perf-meters-root"
	role="group"
	aria-label="Performance readouts"
	bind:this={rootEl}
	onpointerenter={() => _cardEvent('enter')}
	onpointerleave={() => _cardEvent('leave')}
>
	<button
		type="button"
		class="perf-meters-compact"
		data-custom-tip=""
		aria-expanded={cardOpen}
		aria-controls="perf-meters-panel"
		aria-pressed={cardState.pinned}
		onclick={() => _cardEvent('toggle')}
	>
		<span class="perf-meter" class:warn={hzDisplayLevel === 'warn'} class:crit={hzDisplayLevel === 'crit'}>{hzText}</span>
		<span class="perf-meter cache-n">{cacheN}</span>
		<span class="perf-meter" class:warn={waveformStutterWarn}>{waveformText}</span>
		<span
			class="perf-meter memory-mb"
			class:warn={memoryLevel === 'warn'}
			class:crit={memoryLevel === 'crit'}
		>{memoryText}</span>
	</button>

	{#if cardOpen}
		<div id="perf-meters-panel" class="perf-meters-panel" role="region" aria-label="Performance readout details">
			<dl class="perf-readout-rows">
				{#each readoutRows as row (row.key)}
					<div class="perf-readout-row" data-readout={row.key}>
						<dt class="perf-readout-name">{row.name}</dt>
						<dd class="perf-readout-value">{row.value}</dd>
						<dd class="perf-readout-explainer">{row.explainer}</dd>
						<dd class="perf-readout-detail">{row.detail}</dd>
					</div>
				{/each}
			</dl>
			<p class="perf-readout-pin-hint">
				{cardState.pinned
					? 'Pinned. Click the readouts again, press Escape or click outside to close.'
					: 'Click the readouts to pin this card and see the history and resets.'}
			</p>
		{#if monitorOpen}
			<div class="perf-meters-charts">
				<PerfMeterSpark values={hzChart} label="Hz" title="Presentation publish rate over recent samples." />
				<PerfMeterSpark values={cacheChart} label="Cache MB" title="Prefetch + ANLZ cache footprint over recent samples." />
				<PerfMeterSpark values={memoryChart} label="Memory MB" title="JS heap + decoded PCM over recent samples." />
			</div>
			<ul class="perf-meters-breakdown">
				{#each breakdown as line (line.key)}
					<li class="perf-breakdown-row" title={line.title}>
						<span class="perf-breakdown-label">{line.label}</span>
						<span class="perf-breakdown-value">{line.value}</span>
						{#if line.reset}
							<button
								type="button"
								class="perf-breakdown-reset"
								onclick={() => _handleReset(line.reset!)}
							>Reset</button>
						{/if}
					</li>
				{/each}
			</ul>
			<p
				class="perf-meters-chrome-tm-hint"
				title="Chromium sessions only. Capture renderer vs GPU vs Browser per docs/perf/performance-register.md. Packaged WKWebView has no Chrome Task Manager."
			>
				For Chromium dev sessions, use Chrome Task Manager (Shift+Esc) to capture
				tab-renderer CPU and memory; see the performance register.
			</p>
		{/if}
		</div>
	{/if}
</div>

<style>
	.perf-meters-root {
		position: relative;
		display: inline-flex;
	}
	.perf-meters-compact {
		display: inline-flex;
		align-items: center;
		gap: 0;
		padding: 0;
		margin: 0;
		border: none;
		background: transparent;
		cursor: pointer;
		font: inherit;
	}
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
	.perf-meters-panel {
		position: absolute;
		top: 100%;
		left: 0;
		z-index: 200;
		margin-top: 4px;
		padding: 8px;
		min-width: 320px;
		max-width: min(420px, calc(100vw - 32px));
		background: var(--rb-surface, #1a1a1a);
		border: 1px solid var(--rb-border, #333);
		border-radius: 4px;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
	}
	.perf-readout-rows {
		margin: 0 0 6px;
		display: grid;
		gap: 6px;
		font-size: 11px;
	}
	.perf-readout-row {
		display: grid;
		grid-template-columns: 1fr auto;
		column-gap: 8px;
	}
	.perf-readout-name {
		color: var(--rb-text, #ddd);
		font-weight: 600;
	}
	.perf-readout-value {
		margin: 0;
		font-variant-numeric: tabular-nums;
		text-align: right;
		color: var(--rb-text, #ddd);
	}
	.perf-readout-explainer,
	.perf-readout-detail {
		grid-column: 1 / -1;
		margin: 1px 0 0;
		color: var(--rb-text-dim);
		line-height: 1.35;
	}
	.perf-readout-detail {
		font-size: 10px;
		opacity: 0.8;
	}
	.perf-readout-pin-hint {
		margin: 4px 0 8px;
		font-size: 10px;
		color: var(--rb-text-dim);
	}
	.perf-meters-charts {
		display: flex;
		gap: 8px;
		margin-bottom: 8px;
	}
	.perf-meters-breakdown {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 10px;
	}
	.perf-breakdown-row {
		display: grid;
		grid-template-columns: 1fr auto auto;
		gap: 6px;
		align-items: center;
		padding: 2px 0;
		color: var(--rb-text-dim);
	}
	.perf-breakdown-label {
		text-align: left;
	}
	.perf-breakdown-value {
		font-variant-numeric: tabular-nums;
		text-align: right;
	}
	.perf-breakdown-reset {
		font-size: 9px;
		padding: 1px 4px;
		border: 1px solid var(--rb-border, #444);
		border-radius: 2px;
		background: transparent;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.perf-breakdown-reset:hover {
		border-color: var(--rb-text-dim);
	}
	.perf-meters-chrome-tm-hint {
		margin: 8px 0 0;
		font-size: 10px;
		color: var(--rb-text-dim);
		line-height: 1.4;
	}
</style>
