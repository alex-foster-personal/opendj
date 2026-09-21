<script lang="ts">
	// Strip overview waveform (~20px, SCREENSHOT-SPEC 3): native 400-point
	// PWAV preview bands from /anlz, position marker from the engine clock,
	// cue letters + memory markers, the ENGAGED loop band, stored-loop in/out
	// time chips, click-to-seek.
	// The engaged-loop band and the stored-loop chips are different things:
	// the chips come from hot-cue rows saved in rekordbox, the band is the
	// loop the engine is running right now. Only the chips existed until
	// DECKUX-04, so a live loop showed on the wavestack row and nowhere else.
	// No analysis -> explicit 'NO ANALYSIS' state; empty deck -> blank strip.
	// Never synthesized waveforms (COMPONENT-MAP 1.3).
	// Vocal blue bars (SPIKE-B1): 2px-ish top layer, drawn ONLY for real
	// PVDI regions (status 'rekordbox'); the two barless states surface as
	// explicit tooltips - three mandatory states, nothing invented.
	// Scrub-hover lyrics: hovering shows the word under the pointer above the strip.
	import ScrubLyricStrip from '$lib/components/lyrics/ScrubLyricStrip.svelte';
	import { cancelHoverLoad, hoverLoadLyrics, lyricEntry } from '$lib/lyrics/lyrics-cache.svelte';
	import {
		indexLyricWords,
		nearSecondsForScale,
		resolvePointerWord,
		timeForPointer,
		type PointerWord,
		type WordIndex
	} from '$lib/lyrics/pointer-word';
	import { vocalsOf, type Vocals } from '$lib/rb/api-rb';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { keyAtPlayheadNow } from '$lib/player/key-playhead-lazy.svelte';
	import type { DeckState } from '$lib/rb/deck-state-types';
	import type { HotCueSlot } from '$lib/rb/hot-cue-types';
	import { drawStripWaveform } from './strip-waveform-render';
	import { shouldShowNativeGridMarker } from './strip-native-grid';

	let {
		deck,
		pending,
		onSeek,
		onPlay
	}: {
		deck: DeckState;
		pending: boolean;
		onSeek: (ms: number) => Promise<void>;
		/** Q1: carries the originating click's own `event.timeStamp`. */
		onPlay: (pressT0Ms?: number, quantize?: boolean) => Promise<void>;
	} = $props();

	// Canvas backing resolution (CSS scales to 100% x var(--rb-strip-h)).
	const W = 400;
	const H = 40;

	let canvas: HTMLCanvasElement | undefined = $state();
	/** Play affordance above the last paused seek point (pct along strip). */
	let playHintPct: number | null = $state(null);

	const scrubOn: boolean = $derived(
		uiPrefs.lyrics_global && uiPrefs.lyrics_hover_scrub && deck.stable_id !== null
	);
	const wordIndex: WordIndex | null = $derived.by(() => {
		if (!scrubOn || deck.stable_id === null) return null;
		const entry = lyricEntry(deck.stable_id);
		if (entry === null || entry.state !== 'loaded' || entry.track === null) return null;
		if (entry.track.words.length === 0) return null;
		return indexLyricWords(entry.track.words);
	});

	let hoverX: number | null = $state(null);
	let stripW: number = $state(0);
	let pointer: PointerWord | null = $state(null);

	const posPct: number = $derived(
		deck.duration_ms === null || deck.duration_ms === 0
			? 0
			: (deck.position_ms / deck.duration_ms) * 100
	);

	// Validated vocals of the loaded analysis; null while no anlz payload.
	const vocals: Vocals | null = $derived(deck.anlz === null ? null : vocalsOf(deck.anlz));

	const vocalsTitle: string | null = $derived.by(() => {
		if (vocals === null) return null;
		if (vocals.status === 'no_vocals') return 'no vocals detected';
		else if (vocals.status === 'not_analyzed') return 'vocals not analyzed in rekordbox';
		else if (vocals.status === 'demucs') return 'vocals: local detection';
		else return null; // rekordbox: the bars themselves are the signal
	});

	const memoryCuesMs: number[] = $derived(
		deck.anlz === null
			? []
			: deck.anlz.cues.filter((c) => c.kind === 'memory').map((c) => c.in_ms)
	);

	// PARITY-02: OWN selected but this track has no own analysis to serve -
	// the backend already returns the real empty grid plus this reason
	// (rb_assets.py _resolve_beatgrid_source); nothing previously read it, so
	// the strip went inert with no explanation beyond the toggle itself
	// (discussion_r3921839841).
	const ownGridUnavailable: string | null = $derived(
		deck.anlz?.beatgrid_source === 'own' ? deck.anlz.beatgrid_own_unavailable_reason : null
	);
	const showNativeGridMarker: boolean = $derived(shouldShowNativeGridMarker(deck.anlz));

	// First stored loop in the hot-cue bank -> in/out time chips (display-only
	// at v1 per COMPONENT-MAP 1.3; sparse coverage is real).
	const loopCue: { slot: HotCueSlot; in_ms: number; out_ms: number } | null = $derived.by(() => {
		for (const hc of deck.hot_cues) {
			if (hc.is_loop && hc.out_ms !== null) {
				return { slot: hc.slot, in_ms: hc.in_ms, out_ms: hc.out_ms };
			}
		}
		return null;
	});

	const loopCues: { in_ms: number; out_ms: number }[] = $derived(
		deck.hot_cues.flatMap((hc) => (hc.is_loop && hc.out_ms !== null ? [{ in_ms: hc.in_ms, out_ms: hc.out_ms }] : []))
	);

	const keySegmentMarkersS: readonly number[] = $derived(
		keyAtPlayheadNow(deck.anlz, deck.position_ms, deck.key_shift_semitones, deck.key).markerTimesS
	);

	// ----------------------------------------------------------- _helpers

	function _pctOf(ms: number): number {
		if (deck.duration_ms === null || deck.duration_ms === 0) return 0;
		return (ms / deck.duration_ms) * 100;
	}

	function _fmtMmSs(ms: number): string {
		const totalS = Math.max(0, Math.floor(ms / 1000));
		const m = Math.floor(totalS / 60);
		const s = totalS % 60;
		return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
	}

	// Every value read here is a tracked dependency, so the strip repaints
	// when the engaged loop changes - not only when the analysis arrives.
	$effect(() => {
		const c = canvas;
		if (c === undefined) return;
		const ctx = c.getContext('2d');
		if (ctx === null) throw new Error('StripWaveform: canvas 2d context unavailable');
		drawStripWaveform(ctx, {
			widthPx: W,
			heightPx: H,
			durationMs: deck.duration_ms,
			waveform: deck.anlz === null ? null : deck.anlz.waveform,
			vocals,
			loop: deck.loop,
			loopCues
		});
	});

	$effect(() => {
		if (deck.playing || deck.stable_id === null) playHintPct = null;
	});

	function _pointAt(e: MouseEvent): { x: number; t: number } | null {
		if (deck.duration_ms === null || deck.duration_ms === 0) return null;
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		if (rect.width <= 0) return null;
		stripW = rect.width;
		const x = Math.min(rect.width, Math.max(0, e.clientX - rect.left));
		return { x, t: timeForPointer(x, rect.width, deck.duration_ms / 1000) };
	}

	function _resolveAt(t: number): PointerWord | null {
		if (wordIndex === null || deck.duration_ms === null || stripW <= 0) return null;
		return resolvePointerWord(wordIndex, t, {
			nearS: nearSecondsForScale(deck.duration_ms / 1000 / stripW)
		});
	}

	function handleStripPointerMove(e: PointerEvent): void {
		const point = _pointAt(e);
		if (point === null) return;
		hoverX = point.x;
		if (scrubOn && deck.stable_id !== null && uiPrefs.lyrics_load_strategy === 'hover') {
			hoverLoadLyrics(deck.stable_id);
		}
		pointer = _resolveAt(point.t);
	}

	function handleStripPointerLeave(): void {
		hoverX = null;
		pointer = null;
		if (deck.stable_id !== null) cancelHoverLoad(deck.stable_id);
	}

	async function handleClick(e: MouseEvent): Promise<void> {
		if (deck.duration_ms === null) return;
		const el = e.currentTarget as HTMLElement;
		const rect = el.getBoundingClientRect();
		const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
		const state = _resolveAt(timeForPointer(ratio * rect.width, rect.width, deck.duration_ms / 1000));
		if (
			state !== null &&
			state.kind === 'inside' &&
			state.focusStartS !== null
		) {
			await onSeek(state.focusStartS * 1000);
			if (!deck.playing) playHintPct = (state.focusStartS * 1000 / deck.duration_ms) * 100;
			return;
		}
		await onSeek(ratio * deck.duration_ms);
		if (!deck.playing) playHintPct = ratio * 100;
	}

	async function handlePlayHint(e: MouseEvent): Promise<void> {
		e.stopPropagation();
		playHintPct = null;
		await onPlay(e.timeStamp, e.metaKey || e.ctrlKey);
	}
</script>

<div class="strip-wrap">
	{#if playHintPct !== null && !deck.playing && deck.stable_id !== null}
		<button
			type="button"
			class="play-hint"
			style={`left:${playHintPct}%`}
			aria-label={`play from waveform deck ${deck.deck_id}`}
			data-testid={`waveform-play-deck-${deck.deck_id}`}
			title="Play"
			onclick={(e) => void handlePlayHint(e)}
		>
			▶
		</button>
	{/if}
	<button
		class="strip"
		onclick={(e) => void handleClick(e)}
		onpointermove={handleStripPointerMove}
		onpointerleave={handleStripPointerLeave}
		disabled={deck.stable_id === null || pending}
		aria-label={`waveform seek deck ${deck.deck_id}`}
		data-testid={`waveform-seek-deck-${deck.deck_id}`} data-wave-surface="strip"
		title={vocalsTitle ?? undefined}
	>
		<canvas bind:this={canvas} width={W} height={H}></canvas>

		{#if deck.anlz_error !== null}
			<span class="no-anlz" title="No rekordbox ANLZ for this track - the strip has no waveform, beatgrid, or cue overlay">NO ANALYSIS</span>
		{:else if ownGridUnavailable !== null}
			<span class="no-anlz" title={ownGridUnavailable}>NO OWN GRID</span>
		{:else if showNativeGridMarker}
			<span class="native-grid" data-testid="native-grid-marker" title="Measured native beatgrid; rekordbox has no AnalysisDataPath.">native grid</span>
		{/if}

		{#each memoryCuesMs as ms, i (i)}
			<span class="mem-cue" style={`left:${_pctOf(ms)}%`}></span>
		{/each}

		{#each keySegmentMarkersS as atS, i (i)}
			<span
				class="key-seg-marker"
				style={`left:${_pctOf(atS * 1000)}%`}
				title={`key change at ${_fmtMmSs(atS * 1000)}`}
			></span>
		{/each}

		{#each deck.hot_cues as hc (hc.slot)}
			{#if !(hc.is_loop && hc.out_ms !== null)}
				<span class="cue-letter" data-testid={`deck-cue-letter-${deck.deck_id}`} data-cue-slot={hc.slot} style={`left:${_pctOf(hc.in_ms)}%`}>{hc.slot}</span>
			{/if}
		{/each}

		{#if loopCue !== null}
			<span class="loop-chip in" style={`left:${_pctOf(loopCue.in_ms)}%`} title={`Loop in at ${_fmtMmSs(loopCue.in_ms)} (hot cue ${loopCue.slot})`}>
				{loopCue.slot} {_fmtMmSs(loopCue.in_ms)}
			</span>
			<span class="loop-chip out" style={`left:${_pctOf(loopCue.out_ms)}%`} title={`Loop out at ${_fmtMmSs(loopCue.out_ms)}`}>
				{_fmtMmSs(loopCue.out_ms)}
			</span>
		{/if}

		{#if deck.stable_id !== null}
			<span class="pos" style={`left:${posPct}%`}></span>
		{/if}
	</button>

	{#if wordIndex !== null && pointer !== null && hoverX !== null && stripW > 0}
		<ScrubLyricStrip index={wordIndex} state={pointer} xPx={hoverX} widthPx={stripW} />
	{/if}
</div>

<style>
	.strip-wrap {
		position: relative;
		width: 100%;
		flex: 0 0 auto;
		overflow: visible;
		z-index: 2;
	}
	.play-hint {
		position: absolute;
		bottom: calc(100% + 2px);
		transform: translateX(-50%);
		z-index: 5;
		width: 22px;
		height: 22px;
		padding: 0;
		border: 1px solid rgba(236, 240, 244, 0.65);
		border-radius: 50%;
		background: color-mix(in srgb, var(--rb-panel-raised) 70%, #000);
		color: #fff;
		font-size: 11px;
		line-height: 1;
		cursor: pointer;
		box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45);
	}
	.play-hint:hover {
		background: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.strip {
		position: relative;
		display: block;
		width: 100%;
		height: var(--rb-strip-h);
		min-height: var(--rb-strip-h);
		padding: 0;
		margin: 0;
		background: #080a0d;
		border: 1px solid var(--rb-border);
		cursor: pointer;
		overflow: hidden;
	}
	.strip:disabled {
		cursor: default;
	}
	canvas {
		display: block;
		width: 100%;
		height: 100%;
	}
	.no-anlz {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		letter-spacing: 1px;
	}
	.native-grid {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: var(--rb-fs-label);
		color: var(--rb-green);
		letter-spacing: 1px;
		pointer-events: none;
	}
	.mem-cue {
		position: absolute;
		top: 0;
		width: 0;
		height: 0;
		margin-left: -3px;
		border-left: 3px solid transparent;
		border-right: 3px solid transparent;
		border-top: 4px solid var(--rb-red);
	}
	.key-seg-marker {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 1px;
		margin-left: -1px;
		background: rgba(230, 180, 60, 0.85);
	}
	.cue-letter {
		position: absolute;
		top: 0;
		margin-left: -1px;
		padding: 0 1px;
		font-size: 8px;
		line-height: 9px;
		color: var(--rb-bg);
		background: var(--rb-green);
		border-radius: 1px;
	}
	.loop-chip {
		position: absolute;
		bottom: 0;
		margin-left: 1px;
		padding: 0 2px;
		font-size: 8px;
		line-height: 10px;
		font-variant-numeric: tabular-nums;
		border-radius: 1px;
		white-space: nowrap;
	}
	.loop-chip.in {
		background: var(--rb-panel-raised);
		color: var(--rb-text);
		border: 1px solid var(--rb-border);
	}
	.loop-chip.out {
		background: var(--rb-green);
		color: #04120a;
	}
	.pos {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 1px;
		background: #ffffff;
		box-shadow: 0 0 3px rgba(255, 255, 255, 0.7);
	}
</style>
