<script lang="ts">
	// CMDK-07 / CMDK-10: the highlighted track's overview, drawn from data the
	// library already holds (no decode). Top to bottom: the section ribbon, the
	// overview waveform with hot cues as lettered flags, and a thin vocal lane.
	// The "above" layout (option B) sits over the bar; "inline" (option C, its
	// own flag) is a slimmer copy drawn inside the highlighted row. The cue
	// preview's playhead and the bar the last jump landed on are DOM overlays,
	// so the canvas is painted once per track and data change, never per tick.
	import type { AnlzData } from '$lib/rb/cmdk-preview-grid';
	import type { PreviewAssets } from '$lib/rb/cmdk-preview-data.svelte';
	import { previewStripDataToStripBands } from '$lib/rb/deck-strip-preview';
	import type { WaveformDesign } from '$lib/rb/waveform-design';
	import { drawStripPreviewBands } from '../deck/strip-waveform-render';
	import { readPalette } from '../wave/cues';
	import { stripHotCues } from '$lib/rb/cmdk-preview-grid';
	import { sectionSpans } from '../wave/sections';

	let {
		variant,
		title,
		assets,
		anlz,
		duration_s,
		design,
		playhead = null,
		anchor = null,
		message,
		palette,
		onpainted
	}: {
		variant: 'above' | 'inline';
		title: string;
		/** Strip and vocals; undefined while the dwell has not asked yet. */
		assets: PreviewAssets | undefined;
		/** Sections, hot cues and the beat grid, when the shared /anlz cache holds them. */
		anlz: AnlzData | null;
		duration_s: number | null;
		design: WaveformDesign;
		/** The cue preview's playhead as a 0..1 fraction, null when it is not sounding this track. */
		playhead?: number | null;
		/** Where the last jump landed, as a 0..1 fraction. */
		anchor?: number | null;
		/** One line under the title: where the preview is, or how to start it. */
		message: string;
		/** Changes whenever the --rb-wave-* vars do (theme, wave palette, skin), so the strip repaints. */
		palette: string;
		/** Called after a paint that drew the waveform, for the first-paint budget. */
		onpainted?: () => void;
	} = $props();

	/**
	 * Heights in CSS px. Both layouts draw every layer (CMDK-10: the same overview inside the
	 * row): the inline one scales the ribbon, wave, lane and cue flags down to fit the row
	 * (Sol review of 8e53c84f1). `flag` is the cue flag's side, `label` the ribbon's text size.
	 */
	const g = $derived(
		variant === 'above'
			? { H: 64, ribbonH: 13, waveTop: 15, waveH: 41, vocalTop: 59, vocalH: 5, flag: 11, font: 9, label: 9 }
			: { H: 32, ribbonH: 7, waveTop: 8, waveH: 19, vocalTop: 28, vocalH: 3, flag: 8, font: 7, label: 0 }
	);

	let canvas = $state<HTMLCanvasElement | null>(null);
	let width = $state(0);

	const strip = $derived(assets?.status === 'ready' ? assets.strip : null);
	const vocals = $derived(assets?.status === 'ready' ? assets.vocals : null);
	const hotCues = $derived(anlz === null ? [] : stripHotCues(anlz.cues));
	const sectionsShown = $derived(anlz === null ? [] : sectionSpans(anlz.phrases));
	const view = $derived(
		assets === undefined || assets.status === 'loading'
			? 'loading'
			: assets.status === 'error'
				? 'error'
				: strip === null
					? 'no-strip'
					: 'ready'
	);

	$effect(() => {
		if (canvas === null || width <= 0 || view !== 'ready' || strip === null) return;
		// paint() snapshots the --rb-wave-* vars, which the theme, the wave palette and the
		// skin all swap, so a change to any of them repaints (Codex r4275811416). The key comes
		// in as a prop rather than an import, to keep prefs.svelte's fan-in where it is.
		void palette;
		paint(canvas, width);
	});

	function paint(el: HTMLCanvasElement, w: number): void {
		const dpr = window.devicePixelRatio || 1;
		el.width = Math.round(w * dpr);
		el.height = Math.round(g.H * dpr);
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('CmdkPreviewStrip: 2d canvas context unavailable');
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, w, g.H);
		const paletteRootEl = document.querySelector('.perf-root');
		const paletteRoot =
			paletteRootEl instanceof HTMLElement ? paletteRootEl : el;
		const palette = readPalette(paletteRoot);
		const dur = duration_s;
		const xOf = (sec: number): number => (dur !== null && dur > 0 ? (sec / dur) * w : 0);
		if (g.ribbonH > 0 && dur !== null && dur > 0) {
			ctx.font = '600 9px var(--rb-font-label, system-ui, sans-serif)';
			ctx.textBaseline = 'middle';
			for (const span of sectionsShown) {
				const x0 = xOf(span.start_s);
				const sw = xOf(span.end_s) - x0;
				ctx.globalAlpha = 0.55;
				ctx.fillStyle = palette.sections[span.family];
				ctx.fillRect(x0 + 1, 0, Math.max(0, sw - 2), g.ribbonH - 1);
				ctx.globalAlpha = 1;
				if (g.label > 0 && sw > 46) {
					ctx.fillStyle = palette.sections.text;
					ctx.fillText(span.label, x0 + 5, g.ribbonH / 2);
				}
			}
		}
		if (strip !== null && strip.max > 0) {
			const bands = previewStripDataToStripBands(strip);
			ctx.save();
			ctx.translate(0, g.waveTop);
			drawStripPreviewBands(ctx, bands.preview, bands.kind, w, g.waveH, design, {
				low: palette.low,
				mid: palette.mid,
				high: palette.high,
				mono: palette.mono,
				vocal: palette.vocal
			});
			ctx.restore();
		}
		if (vocals !== null && (vocals.status === 'rekordbox' || vocals.status === 'demucs') && dur !== null && dur > 0) {
			ctx.fillStyle = palette.vocal;
			for (const region of vocals.regions) {
				const x0 = Math.max(0, xOf(region.start_s));
				const x1 = Math.min(w, xOf(region.end_s));
				if (x1 > x0) ctx.fillRect(x0, g.vocalTop, x1 - x0, g.vocalH);
			}
		}
		if (dur !== null && dur > 0) {
			ctx.font = `700 ${g.font}px var(--rb-font-label, system-ui, sans-serif)`;
			ctx.textAlign = 'center';
			ctx.textBaseline = 'middle';
			for (const cue of hotCues) {
				const x = xOf(cue.in_ms / 1000);
				// A hot loop in the bank keeps its letter, in the loop color, with its span shaded.
				const loop = cue.is_loop && cue.out_ms !== null && cue.out_ms > cue.in_ms;
				ctx.fillStyle = cue.is_loop ? palette.cueLoop : palette.cueHotCue;
				if (loop) {
					ctx.globalAlpha = 0.3;
					ctx.fillRect(x, g.waveTop, Math.max(1, xOf((cue.out_ms as number) / 1000) - x), g.waveH);
					ctx.globalAlpha = 1;
				}
				ctx.fillRect(x - 0.5, g.waveTop, 1, g.waveH);
				ctx.fillRect(x - 0.5, g.waveTop, g.flag, g.flag);
				ctx.fillStyle = palette.bg;
				ctx.fillText(cue.slot as string, x + g.flag / 2 - 0.5, g.waveTop + g.flag / 2 + 0.5);
			}
			ctx.textAlign = 'start';
		}
		if (strip !== null) onpainted?.();
	}

	const pct = (ratio: number | null): string | null =>
		ratio === null || !Number.isFinite(ratio) ? null : `${Math.min(100, Math.max(0, ratio * 100))}%`;
</script>

<div
	class="pv {variant}"
	data-testid={variant === 'above' ? 'command-bar-preview' : 'command-bar-preview-inline'}
	data-state={view}
>
	{#if variant === 'above'}
		<div class="pv-head">
			<span class="pv-title">{title}</span>
			<span class="pv-msg" data-testid="command-bar-preview-message">{message}</span>
		</div>
	{/if}
	<div class="pv-body" bind:clientWidth={width} style:height={`${g.H}px`}>
		<canvas bind:this={canvas} style:height={`${g.H}px`} aria-hidden="true"></canvas>
		{#if pct(anchor) !== null}
			<span class="pv-anchor" style:left={pct(anchor)} title="Where the last jump landed"></span>
		{/if}
		{#if pct(playhead) !== null}
			<span class="pv-head-line" style:left={pct(playhead)} data-testid="command-bar-preview-playhead"></span>
		{/if}
		{#if view !== 'ready'}
			<span class="pv-note" data-testid={variant === 'above' ? undefined : 'command-bar-preview-inline-note'}>
				{view === 'loading'
					? 'Loading the overview...'
					: view === 'error'
						? `No overview: ${assets?.status === 'error' ? assets.message : ''}`
						: 'Not analyzed yet, so there is no overview to draw'}
			</span>
		{/if}
	</div>
	{#if variant === 'inline' && message !== ''}
		<!-- The cue status shows in the row too: a missing grid or a refused preview names its reason (CMDK-08). -->
		<span class="pv-msg inline-msg" data-testid="command-bar-preview-inline-message">{message}</span>
	{/if}
</div>

<style>
	.pv {
		display: grid;
		gap: 4px;
	}
	.pv.above {
		padding: 8px 10px;
		background: var(--rb-panel-raised, #1b1f26);
		border: 1px solid var(--rb-border, #2c323c);
		border-radius: 8px;
		box-shadow: 0 10px 30px rgb(0 0 0 / 35%);
		color: var(--rb-text, #e6e9ee);
	}
	.pv-head {
		display: flex;
		justify-content: space-between;
		gap: 12px;
		font-size: 11px;
		min-width: 0;
	}
	.pv-title {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 600;
	}
	.pv-msg {
		flex: none;
		color: var(--rb-text-dim, #8a94a3);
	}
	.inline-msg {
		font-size: 10px;
		line-height: 1.2;
	}
	.pv-body {
		position: relative;
		min-width: 0;
	}
	canvas {
		display: block;
		width: 100%;
	}
	.pv-anchor,
	.pv-head-line {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 2px;
		margin-left: -1px;
		pointer-events: none;
	}
	.pv-anchor {
		background: var(--rb-cmdk-pick, #a46ef0);
		opacity: 0.7;
	}
	.pv-head-line {
		background: var(--rb-accent, #e6e9ee);
	}
	.pv-note {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 11px;
		color: var(--rb-text-dim, #8a94a3);
	}
	.inline .pv-note {
		font-size: 10px;
	}
</style>
