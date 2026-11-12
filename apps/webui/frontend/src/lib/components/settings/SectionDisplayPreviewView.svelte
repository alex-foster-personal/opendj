<script lang="ts">
	import type { SectionOverlayPrefsState } from '$lib/rb/section-overlay-prefs';
	import type { SectionsPreviewPaintFn } from '$lib/settings/sections-preview-paint';

	let {
		paint,
		prefs,
		label = 'Segment display preview'
	}: {
		paint: SectionsPreviewPaintFn;
		prefs: SectionOverlayPrefsState;
		label?: string;
	} = $props();

	const PREVIEW_HEIGHT_PX = 72;
	let canvas: HTMLCanvasElement | undefined = $state();
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
		if (ctx === null) throw new Error('section display preview: canvas 2d context unavailable');
		const dpr = window.devicePixelRatio || 1;
		el.width = Math.round(cssWidth * dpr);
		el.height = Math.round(PREVIEW_HEIGHT_PX * dpr);
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		paint(ctx, cssWidth, PREVIEW_HEIGHT_PX, prefs);
	});
</script>

<div class="sec-prev" aria-label={label}>
	<canvas bind:this={canvas} class="sec-prev-canvas" width="320" height={PREVIEW_HEIGHT_PX}
	></canvas>
</div>

<style>
	.sec-prev {
		width: 100%;
		margin-top: 6px;
	}
	.sec-prev-canvas {
		display: block;
		width: 100%;
		height: 72px;
		border-radius: 4px;
		background: var(--rb-panel, #0a0c10);
	}
</style>
