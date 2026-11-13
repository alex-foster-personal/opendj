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
	import { memoryReadout } from '$lib/rb/memory-meter-model';
	import { hasJsHeapApi, readJsHeapMB } from '$lib/rb/js-heap-reading';
	import { readMachinePressure } from '$lib/rb/machine-pressure';
	import {
		fetchProcessFamilySnapshot,
		processFamilyRefreshDue,
		readFreshProcessFamilySnapshot,
		readUnavailableProbeStale
	} from '$lib/rb/process-family-snapshot';
	import {
		breakdownLines,
		type ChartSample,
		perfMeterSampleIntervalMs,
		type ProcessFamilySnapshot,
		pushSample
	} from '$lib/rb/perf-meter-model';
	import { readPerfEvents, recordPerfEvent, resetPerfEventLog } from '$lib/rb/perf-event-log';
	import {
		isPerfCardOpen,
		nextPerfCardState,
		PERF_CARD_CLOSED,
		perfReadoutRows,
		setPerfReadoutRowsSource,
		type PerfCardEvent,
		type PerfCardState
	} from '$lib/rb/perf-readout-card';
	import { nativeShellKind } from '$lib/shell/native-shell';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
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
	// the figure the meter last showed, so the chart plots the same number
	let memoryTotalMB = 0;
	let chartHistory = $state<ChartSample[]>([]);
	let hzDriftLogged = $state(false);

	const HEAP_API_PRESENT = typeof performance !== 'undefined' && hasJsHeapApi(performance);

	const hzDisplayLevel = $derived(
		!hzQuality.ok ? 'crit' : hzLevel === 'warn' ? 'warn' : hzLevel === 'crit' ? 'crit' : 'ok'
	);

	// The processes the pinned breakdown lists: re-read through the expiry rule
	// on every tick and every fetch, so once the snapshot ages out the card
	// stops listing it, as the compact figure does.
	let breakdownProcesses = $state<ProcessFamilySnapshot | null>(null);

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
			kernelLevel: breakdownProcesses?.kernelLevel ?? null,
			churnScore: breakdownProcesses?.churnScore ?? null,
			compressorRate: breakdownProcesses?.compressorRate ?? null,
			processes: breakdownProcesses,
			shellKind: nativeShellKind()
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

	// PERFREADOUT-12579: the sampler reads these rows (lazily, only when it
	// posts) so agents get the card's own rows. The body reads no state, so it
	// registers once and unregisters on unmount.
	$effect(() => setPerfReadoutRowsSource(() => readoutRows));

	// PERF-UI-12: each row draws its own history beside its value. These are
	// read only inside the open card, so a closed card computes none of them.
	const rowCharts = $derived<Record<string, Array<number | null>>>({
		hz: chartHistory.map((s) => s.hz),
		cache: chartHistory.map((s) => s.cacheN),
		memory: chartHistory.map((s) => s.memoryMB)
	});
	const rowLevels = $derived<Record<string, 'ok' | 'warn' | 'crit'>>({
		hz: hzDisplayLevel,
		cache: 'ok',
		waveform: waveformStutterWarn ? 'warn' : 'ok',
		memory: memoryLevel
	});

	// The engine's process walk (engine, odj-audio and other sidecars, desktop
	// shell). A snapshot older than PROCESS_SNAPSHOT_MAX_AGE_MS is not shown
	// as current: the figure then says the processes are unavailable.
	const PROCESS_SNAPSHOT_MAX_AGE_MS = 30_000;
	// The closed bar refreshes the walk this often (it walks every process on
	// the machine, so not on every 2 s tick).
	const PROCESS_REFRESH_MS = 10_000;

	// Every reader that shows the processes as current goes through this one
	// expiry rule: the figure, the pinned breakdown, the chart and the cadence.
	function _freshProcessSnapshot(): ProcessFamilySnapshot | null {
		return readFreshProcessFamilySnapshot(PROCESS_SNAPSHOT_MAX_AGE_MS);
	}

	function _refreshProcesses(force: boolean): void {
		if (!force && !processFamilyRefreshDue(PROCESS_REFRESH_MS)) return;
		void fetchProcessFamilySnapshot().then(() => {
			breakdownProcesses = _freshProcessSnapshot();
			untrack(_updateMemory);
		});
	}

	function _updateMemory(): void {
		const processSnapshot = _freshProcessSnapshot();
		const readout = memoryReadout({
			jsHeapMB: HEAP_API_PRESENT ? readJsHeapMB(performance) : null,
			pcmMB: Math.round(deckPcmEstimatedBytes() / (1024 * 1024)),
			anlzMB: Math.round(anlzCacheEstimatedBytes() / (1024 * 1024)),
			anlzCount: anlzCacheEntryCount(),
			prefetchMB: Math.round(cacheBytes / (1024 * 1024)),
			prefetchCount: cacheN,
			anlzCap: anlzEntryCap(),
			tierLabel: tierLabelForHover(),
			processes: processSnapshot?.members ?? null,
			probeStale: processSnapshot
				? processSnapshot.probeStale
					? { ageSeconds: processSnapshot.probeAgeSeconds ?? null }
					: null
				: readUnavailableProbeStale(PROCESS_SNAPSHOT_MAX_AGE_MS),
			shellKind: nativeShellKind()
		});
		memoryTotalMB = readout.totalMB;
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
		const processes = _freshProcessSnapshot();
		const point: ChartSample = {
			tMs: Date.now(),
			hz: audioHealthHz(),
			cacheN: audioPrefetchReadyCount(),
			cacheMB: Math.round(audioPrefetchReadyBytes() / (1024 * 1024)),
			memoryMB: memoryTotalMB,
			anlzMB: Math.round(anlzCacheEstimatedBytes() / (1024 * 1024)),
			prefetchMB: Math.round(audioPrefetchReadyBytes() / (1024 * 1024)),
			swapMB: pressure?.swapUsedMb ?? null,
			kernelLevel: processes?.kernelLevel ?? null,
			churnScore: processes?.churnScore ?? null
		};
		chartHistory = pushSample(chartHistory, point, CHART_CAP);
	}

	function _sampleTick(): void {
		breakdownProcesses = _freshProcessSnapshot();
		untrack(_updateMemory);
		untrack(_pushChartSample);
	}

	function _kernelAndChurn(): { kernelLevel: number | null; churnScore: number | null } {
		const processes = _freshProcessSnapshot();
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
			_refreshProcesses(true);
			timer = setInterval(() => {
				untrack(_sampleTick);
				_refreshProcesses(true);
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

		// The closed-card tick also records one history point, so hovering the
		// card shows a chart straight away. Measured cost: see PERF-UI-12 in
		// REQUIREMENTS.md. The memory figure counts the engine's processes too,
		// so the closed bar refreshes the process walk, at most every
		// PROCESS_REFRESH_MS (PERF-UI-6785).
		const tick = (): void => {
			untrack(_sampleTick);
			_refreshProcesses(false);
		};
		const start = (): void => {
			if (timer !== null) return;
			tick();
			timer = setInterval(tick, intervalMs);
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

	<!-- PERF-UI-11: fixed and placed from the readouts' rect, because the top
	     bar clips its overflow and an absolute card showed at most 4.5px of 600px. -->
	{#if cardOpen}
		<div id="perf-meters-panel"
			class="perf-meters-panel"
			data-hover-card=""
			role="region"
			aria-label="Performance readout details"
			use:triggerFloatingAction={{ getTrigger: () => rootEl, preferred: 'below', gap: 4 }}
		>
			<dl class="perf-readout-rows">
				{#each readoutRows as row (row.key)}
					<div class="perf-readout-row" data-readout={row.key}>
						<dt class="perf-readout-name">{row.name}</dt>
						<dd
							class="perf-readout-value"
							class:warn={rowLevels[row.key] === 'warn'}
							class:crit={rowLevels[row.key] === 'crit'}
						>{row.value}</dd>
						<dd class="perf-readout-chart">
							{#if rowCharts[row.key] !== undefined}
								<PerfMeterSpark values={rowCharts[row.key]} />
							{/if}
						</dd>
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
				title="Chromium sessions only. Capture renderer vs GPU vs Browser per (private path). Packaged WKWebView has no Chrome Task Manager."
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
	/* Fixed, not absolute: .rb-topbar has overflow: hidden, which clipped the
	   card to the bar's height. triggerFloatingAction sets left/top below the
	   readouts and keeps the card inside the viewport. max-height leaves room
	   for the top bar, so a pinned card taller than the window scrolls below
	   the readouts instead of being clamped up over them. */
	.perf-meters-panel {
		position: fixed;
		top: 0;
		left: 0;
		z-index: 200;
		max-height: calc(100vh - var(--rb-topbar-h, 28px) - 12px);
		overflow-y: auto;
		box-sizing: border-box;
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
	/* PERF-UI-12: value in a thin bordered left column so the numbers are
	   easy to spot, then the name, then that readout's own chart. */
	.perf-readout-row {
		display: grid;
		grid-template-columns: 3.6em 1fr auto;
		column-gap: 8px;
		align-items: start;
	}
	/* dt stays first for valid <dl>; the grid puts the value on the left. */
	.perf-readout-name {
		grid-column: 2;
		grid-row: 1;
		color: var(--rb-text, #ddd);
		font-weight: 600;
	}
	.perf-readout-value {
		grid-column: 1;
		grid-row: 1;
		margin: 0;
		padding: 1px 4px;
		border: 1px solid var(--rb-border, #444);
		border-radius: 3px;
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		text-align: center;
		color: var(--rb-text, #ddd);
	}
	.perf-readout-value.warn {
		color: #e89a3c;
		border-color: #e89a3c;
	}
	.perf-readout-value.crit {
		color: #e05555;
		border-color: #e05555;
	}
	/* A small chart on the name line, so the explainer below keeps its width. */
	.perf-readout-chart {
		margin: 0;
		grid-row: 1;
		grid-column: 3;
	}
	.perf-readout-chart :global(.perf-spark) {
		min-width: 0;
	}
	.perf-readout-chart :global(.perf-spark-svg) {
		width: 75px;
		height: 20px;
	}
	/* Text starts in the name column; the value column stays blank below. */
	.perf-readout-explainer,
	.perf-readout-detail {
		grid-column: 2 / -1;
		margin: 1px 0 0;
		color: var(--rb-text-dim);
		line-height: 1.35;
	}
	/* The memory detail is one line per process; keep its line breaks. */
	.perf-readout-detail {
		font-size: 10px;
		opacity: 0.8;
		white-space: pre-line;
	}
	.perf-readout-pin-hint {
		margin: 4px 0 8px;
		font-size: 10px;
		color: var(--rb-text-dim);
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
