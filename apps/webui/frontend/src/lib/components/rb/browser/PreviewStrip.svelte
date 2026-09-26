<script lang="ts">
	// Browser Preview column strip - INLINE data from the hydrated listing
	// payload (SPIKE-A1/A2 verdicts): tri-band uint8 strip decoded once in
	// BrowserPanel, drawn here per-row on a canvas.
	// Draw policy (SPIKE-A2): draw ONCE when the row first scrolls into
	// view (revealed flips true via the table's IntersectionObserver);
	// redraw only on DPR change or vocal-region arrival.
	// Interactive: hover = thin red scrub line; click = onseek(ratio) if wired.
	// Scrub-hover lyrics: when lyric words are cached, hovering shows the word
	// under the pointer above the strip.
	import ScrubLyricStrip from '$lib/components/lyrics/ScrubLyricStrip.svelte';
	import { cancelHoverLoad, hoverLoadLyrics, lyricEntry } from '$lib/lyrics/lyrics-cache.svelte';
	import { previewCancelWarm, previewWarmOnHover } from '$lib/player/preview-cue.svelte';
	import {
		indexLyricWords,
		nearSecondsForScale,
		resolvePointerWord,
		timeForPointer,
		type PointerWord,
		type WordIndex
	} from '$lib/lyrics/pointer-word';
	import type { PreviewStripData, Vocals } from '$lib/rb/api-rb';
	import type { AnlzData } from '$lib/rb/anlz-types';
	import { previewStripDataToStripBands } from '$lib/rb/deck-strip-preview';
	import type { WaveformDesign } from '$lib/rb/waveform-design';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { drawStripPreviewBands } from '../deck/strip-waveform-render';
	import {
		drawLoopCueBands,
		drawPhraseMarkers,
		drawPointCueMarkers,
		markerBandHeightForSurface,
		readPalette
	} from '../wave/cues';
	import { VOCAL_BLUE, vocalAlpha } from '../wave/render';

	const W = 165;
	const H = 14;
	const PREVIEW_MARKER_BAND_PX = markerBandHeightForSurface(H);
	/** Mini-strip vocal overlay height (CSS px). */
	const VOCAL_BAR_H = 0.7;

	let {
		strip,
		stripLoading = false,
		vocals,
		markerAnlz = null,
		duration_ms,
		revealed,
		nowRatio = null,
		onseek,
		previewing = false,
		onstop,
		stable_id = null,
		enabled = true
	}: {
		strip: PreviewStripData | null;
		/** Resolved by BrowserPanel from listing hydrate + pure cache reads. */
		stripLoading?: boolean;
		vocals: Vocals | null;
		/** The same real ANLZ object used by a loaded deck/main waveform, or an
		 * already-populated shared cache entry. Null deliberately means no
		 * cue/phrase overlay; this component never fetches per virtual row. */
		markerAnlz?: AnlzData | null;
		duration_ms: number | null;
		revealed: boolean;
		/**
		 * Playhead as a 0..1 fraction when this track is loaded on a deck,
		 * null when it is not on any deck. Drawn as a DOM overlay rather
		 * than into the canvas on purpose: the canvas follows the SPIKE-A2
		 * draw-once policy, and repainting it every position tick would
		 * throw that away for every visible row at once.
		 */
		nowRatio?: number | null;
		onseek?: (ratio: number) => void;
		/** CUEOUT-15: this row is the live cue preview, so the playhead is the
		 * preview's rather than a deck's and a stop control is offered. */
		previewing?: boolean;
		onstop?: () => void;
		stable_id?: string | null;
		enabled?: boolean;
	} = $props();

	const scrubOn: boolean = $derived(
		enabled && uiPrefs.lyrics_global && uiPrefs.lyrics_hover_scrub && stable_id !== null
	);
	const wordIndex: WordIndex | null = $derived.by(() => {
		if (!scrubOn || stable_id === null) return null;
		const entry = lyricEntry(stable_id);
		if (entry === null || entry.state !== 'loaded' || entry.track === null) return null;
		if (entry.track.words.length === 0) return null;
		return indexLyricWords(entry.track.words);
	});

	/** Clamped playhead offset in CSS px, or null when off-deck / unusable. */
	const nowX: number | null = $derived(
		nowRatio === null || !Number.isFinite(nowRatio)
			? null
			: Math.min(1, Math.max(0, nowRatio)) * W
	);

	let canvas: HTMLCanvasElement | undefined = $state(undefined);
	let dpr = $state(1);
	let hoverX: number | null = $state(null);

	const title: string | null = $derived.by(() => {
		if (vocals === null) return null;
		if (vocals.status === 'no_vocals') return 'no vocals detected';
		else if (vocals.status === 'not_analyzed') return 'vocals not analyzed in rekordbox';
		else if (vocals.status === 'demucs') return 'vocals: local detection';
		else return null;
	});

	$effect(() => {
		dpr = window.devicePixelRatio || 1;
		const mq = window.matchMedia(`(resolution: ${dpr}dppx)`);
		const onChange = (): void => {
			dpr = window.devicePixelRatio || 1;
		};
		mq.addEventListener('change', onChange);
		return () => mq.removeEventListener('change', onChange);
	});

	$effect(() => {
		const design = uiPrefs.waveform_design;
		if (canvas && strip !== null && revealed) {
			_draw(canvas, strip, vocals, markerAnlz, duration_ms, dpr, design);
		}
	});

	function _draw(
		el: HTMLCanvasElement,
		data: PreviewStripData,
		voc: Vocals | null,
		markerAnlz: AnlzData | null,
		durMs: number | null,
		ratio: number,
		design: WaveformDesign
	): void {
		el.width = W * ratio;
		el.height = H * ratio;
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('PreviewStrip: 2d canvas context unavailable');
		ctx.setTransform(1, 0, 0, 1, 0, 0);
		ctx.scale(ratio, ratio);
		ctx.clearRect(0, 0, W, H);
		if (data.max > 0) {
			const bands = previewStripDataToStripBands(data);
			drawStripPreviewBands(ctx, bands.preview, bands.kind, W, H, design);
		}
		if (
			voc !== null &&
			(voc.status === 'rekordbox' || voc.status === 'demucs') &&
			durMs !== null &&
			durMs > 0
		) {
			ctx.fillStyle = VOCAL_BLUE;
			for (const region of voc.regions) {
				const x0 = Math.max(0, ((region.start_s * 1000) / durMs) * W);
				const x1 = Math.min(W, ((region.end_s * 1000) / durMs) * W);
				if (x1 <= x0) continue;
				ctx.globalAlpha = vocalAlpha(region.intensity);
				ctx.fillRect(x0, 0, x1 - x0, VOCAL_BAR_H);
			}
			ctx.globalAlpha = 1;
		}
		if (markerAnlz !== null && durMs !== null && durMs > 0) {
			const palette = readPalette(el);
			const pxPerS = W / (durMs / 1000);
			// Same order as drawWaveRow: opaque loop spans behind phrase
			// segmentation, point cues in the foreground (LIBUX-12).
			drawLoopCueBands(
				ctx,
				markerAnlz.cues,
				0,
				pxPerS,
				W,
				palette,
				PREVIEW_MARKER_BAND_PX
			);
			drawPhraseMarkers(ctx, markerAnlz.phrases, 0, pxPerS, W, palette);
			drawPointCueMarkers(ctx, markerAnlz.cues, 0, pxPerS, W, palette);
		}
	}

	function _ratioFromEvent(event: MouseEvent): number {
		const el = event.currentTarget as HTMLElement;
		const rect = el.getBoundingClientRect();
		return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
	}

	let pointer: PointerWord | null = $state(null);

	function _resolveAtRatio(ratio: number): PointerWord | null {
		if (wordIndex === null || duration_ms === null || duration_ms <= 0) return null;
		const durationS = duration_ms / 1000;
		return resolvePointerWord(wordIndex, timeForPointer(ratio * W, W, durationS), {
			nearS: nearSecondsForScale(durationS / W)
		});
	}

	function onMove(event: MouseEvent): void {
		const ratio = _ratioFromEvent(event);
		hoverX = ratio * W;
		if (scrubOn && stable_id !== null && uiPrefs.lyrics_load_strategy === 'hover') {
			hoverLoadLyrics(stable_id);
		}
		// CUEOUT-15: a dwell here turns the click's cold fetch into a warm one.
		if (stable_id !== null) previewWarmOnHover(stable_id);
		pointer = _resolveAtRatio(ratio);
	}

	$effect(() => {
		if (wordIndex !== null && hoverX !== null && pointer === null) {
			pointer = _resolveAtRatio(hoverX / W);
		}
	});

	function onLeave(): void {
		hoverX = null;
		pointer = null;
		if (stable_id !== null) {
			cancelHoverLoad(stable_id);
			previewCancelWarm(stable_id);
		}
	}

	function onClick(event: MouseEvent): void {
		event.stopPropagation();
		const ratio = _ratioFromEvent(event);
		const state = _resolveAtRatio(ratio);
		if (
			state !== null &&
			state.kind === 'inside' &&
			state.focusStartS !== null &&
			duration_ms !== null &&
			duration_ms > 0
		) {
			onseek?.(Math.min(1, Math.max(0, (state.focusStartS * 1000) / duration_ms)));
			return;
		}
		onseek?.(ratio);
	}
</script>

{#if strip === null}
	{#if stripLoading}
		<span
			class="strip-loading"
			data-testid="preview-strip-loading"
			aria-busy="true"
			title="Loading waveform preview"
			>…</span
		>
	{:else}
		<span class="no-anlz" title="no preview data (no ANLZ waveform for this track)">-</span>
	{/if}
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="preview-hit"
		data-testid="preview-strip"
		style={`width:${W}px;height:${H}px`}
		title={title ?? 'Click to seek if loaded · hover shows position'}
		onpointermove={onMove}
		onpointerleave={onLeave}
		onclick={onClick}
	>
		<canvas bind:this={canvas} style={`width:${W}px;height:${H}px`}></canvas>
		{#if nowX !== null}
			<span
				class="now"
				class:preview={previewing}
				style={`left:${nowX}px`}
				aria-hidden="true"
			></span>
		{/if}
		{#if previewing && onstop !== undefined}
			<button
				class="stop-preview"
				type="button"
				aria-label="STOP PREVIEW"
				title="Stop the headphone cue preview of this track"
				onclick={(e) => {
					e.stopPropagation();
					onstop?.();
				}}>&#9632;</button
			>
		{/if}
		{#if hoverX !== null}
			<span class="scrub" style={`left:${hoverX}px`} aria-hidden="true"></span>
		{/if}
		{#if wordIndex !== null && pointer !== null && hoverX !== null}
			<ScrubLyricStrip index={wordIndex} state={pointer} xPx={hoverX} widthPx={W} />
		{/if}
	</div>
{/if}

<style>
	.preview-hit {
		position: relative;
		display: inline-block;
		cursor: crosshair;
	}
	/* The preview playhead is deliberately NOT the deck colour: a deck can be
	   on air and this never is, so the two must not read as the same thing. */
	.now.preview {
		background: var(--rb-orange, #f0a030);
		box-shadow: 0 0 3px var(--rb-orange, #f0a030);
	}
	.stop-preview {
		position: absolute;
		top: 0;
		right: 0;
		z-index: 2;
		width: 12px;
		height: 12px;
		padding: 0;
		border: none;
		line-height: 1;
		font-size: 8px;
		color: var(--rb-bg, #111);
		background: var(--rb-orange, #f0a030);
		cursor: pointer;
	}
	canvas {
		display: block;
		pointer-events: none;
	}
	.scrub {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 1px;
		margin-left: -0.5px;
		background: var(--rb-red, #d0342c);
		pointer-events: none;
		z-index: 1;
	}
	/* Live deck position. Sits under the hover scrub so a scrub still
	   reads clearly while the track is playing. */
	.now {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 1px;
		margin-left: -0.5px;
		background: var(--rb-red, #d0342c);
		box-shadow: 0 0 3px var(--rb-red, #d0342c);
		pointer-events: none;
		z-index: 0;
	}
	.no-anlz {
		display: inline-block;
		width: 165px;
		color: var(--rb-text-dim);
		text-align: center;
		line-height: 14px;
	}
	.strip-loading {
		display: inline-block;
		width: 165px;
		color: var(--rb-text-dim);
		text-align: center;
		line-height: 14px;
		letter-spacing: 1px;
	}
</style>
