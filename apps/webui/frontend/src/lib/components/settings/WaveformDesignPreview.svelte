<script lang="ts">
	import type { WaveformDesign } from '$lib/rb/waveform-design';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { resolveWaveBandColors } from '$lib/rb/wave-palette';
	import { effectiveWaveformDesign, effectiveWavePalette } from '$lib/rb/ui-skin';
	import type { AnlzWaveform } from '$lib/rb/anlz-types';
	import { paintWaveformPreview } from '$lib/settings/waveform-preview-paint';

	let { design }: { design?: WaveformDesign } = $props();

	/** CSS height of the preview strip; matches the .wfd-preview rule. */
	const PREVIEW_HEIGHT_PX = 48;

	// The EFFECTIVE look, not the stored token: an 'auto' pref resolves through
	// the active skin, so switching to Gothic repaints this preview as blocks in
	// mono grayscale (Mon 5 Oct 2026).
	const active = $derived(design ?? effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin));
	const palette = $derived(effectiveWavePalette(uiPrefs.wave_palette, uiPrefs.ui_skin));
	// The preview canvas is always painted on a dark fill, so it shows the
	// dark-face hues of the effective band palette (issue #4219): the deck
	// row's own colors (the theme.css mirror), since it paints through the
	// deck row's shading code (W15).
	const colors = $derived(resolveWaveBandColors('dark', palette));

	let canvas: HTMLCanvasElement | undefined = $state();
	/** The canvas's laid-out CSS width: the preview spans the whole settings
	 * card (the maintainer, Tue 6 Oct 2026), so it is measured, not fixed. */
	let cssWidth = $state(0);
	/** The real-track sample, a lazy chunk: only the waveform DATA loads late
	 * (the component itself stays a static import, CHROME-16). A failed load
	 * throws rather than leaving a silently empty preview. */
	let sample: AnlzWaveform | null = $state(null);

	$effect(() => {
		let live = true;
		void import('$lib/settings/waveform-preview-sample').then((m) => {
			if (live) sample = m.previewSampleWaveform();
		});
		return () => {
			live = false;
		};
	});

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
		paintWaveformPreview(ctx, cssWidth, PREVIEW_HEIGHT_PX, active, colors, sample);
	});
</script>

<canvas
	bind:this={canvas}
	class="wfd-preview"
	width="160"
	height={PREVIEW_HEIGHT_PX}
	data-design={active}
	data-palette={palette}
	aria-label="Waveform design preview"
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
