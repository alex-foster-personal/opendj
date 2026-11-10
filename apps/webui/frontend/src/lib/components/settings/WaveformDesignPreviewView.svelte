<script lang="ts">
	// Props-only half of WaveformDesignPreview: the container resolves the
	// effective design and palette from prefs, loads the sample, and injects
	// the painter so this file never imports waveform-preview-paint (Storybook
	// presentational rule).
	import type { WaveformPreviewPaintFn } from '$lib/settings/waveform-preview-paint';

	let {
		paint,
		active,
		palette,
		colors,
		sample,
		gridLines,
		label = 'Waveform design preview'
	}: {
		paint: WaveformPreviewPaintFn;
		active: Parameters<WaveformPreviewPaintFn>[3];
		palette: string;
		colors: Parameters<WaveformPreviewPaintFn>[4];
		sample: Parameters<WaveformPreviewPaintFn>[5];
		gridLines: Parameters<WaveformPreviewPaintFn>[6];
		label?: string;
	} = $props();

	/** CSS height of the preview strip; matches the .wfd-preview rule. */
	const PREVIEW_HEIGHT_PX = 48;

	let canvas: HTMLCanvasElement | undefined = $state();
	/** The canvas's laid-out CSS width: the preview spans the whole settings
	 * card (the maintainer, Tue 6 Oct 2026), so it is measured, not fixed. */
	let cssWidth = $state(0);

	$effect(() => {
		const el = canvas;
		if (el === undefined || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => {
			cssWidth = el.clientWidth;
		});
		ro.observe(el);
		cssWidth = el.clientWidth;
		return () => ro.disconnect();
	});

	$effect(() => {
		const el = canvas;
		if (el === undefined || cssWidth <= 0) return;
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('waveform design preview: canvas 2d context unavailable');
		const dpr = window.devicePixelRatio || 1;
		el.width = Math.round(cssWidth * dpr);
		el.height = Math.round(PREVIEW_HEIGHT_PX * dpr);
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		paint(ctx, cssWidth, PREVIEW_HEIGHT_PX, active, colors, sample, gridLines);
	});
</script>

<canvas
	bind:this={canvas}
	class="wfd-preview"
	width="160"
	height={PREVIEW_HEIGHT_PX}
	data-design={active}
	data-palette={palette}
	data-grid-lines={gridLines}
	aria-label={label}
></canvas>

<style>
	.wfd-preview {
		display: block;
		width: 100%;
		height: 48px;
		margin-top: 8px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		box-sizing: border-box;
	}
</style>
