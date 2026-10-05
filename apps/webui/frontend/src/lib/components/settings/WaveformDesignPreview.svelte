<script lang="ts">
	import type { WaveformDesign } from '$lib/rb/waveform-design';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { resolveStripBandColors } from '$lib/rb/wave-palette';
	import { effectiveWaveformDesign, effectiveWavePalette } from '$lib/rb/ui-skin';
	import { paintWaveformPreview } from '$lib/settings/waveform-preview-paint';

	let { design }: { design?: WaveformDesign } = $props();

	// The EFFECTIVE look, not the stored token: an 'auto' pref resolves through
	// the active skin, so switching to Gothic repaints this preview as blocks in
	// mono grayscale (Mon 5 Oct 2026).
	const active = $derived(design ?? effectiveWaveformDesign(uiPrefs.waveform_design, uiPrefs.ui_skin));
	const palette = $derived(effectiveWavePalette(uiPrefs.wave_palette, uiPrefs.ui_skin));
	// The preview canvas is always painted on a dark fill, so it shows the
	// dark-face hues of the effective band palette (issue #4219).
	const colors = $derived(resolveStripBandColors('dark', palette));

	let canvas: HTMLCanvasElement | undefined = $state();

	$effect(() => {
		const el = canvas;
		if (el === undefined) return;
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('waveform design preview: canvas 2d context unavailable');
		paintWaveformPreview(ctx, el.width, el.height, active, colors);
	});
</script>

<canvas
	bind:this={canvas}
	class="wfd-preview"
	width="160"
	height="36"
	data-design={active}
	data-palette={palette}
	aria-label="Waveform design preview"
></canvas>

<style>
	.wfd-preview {
		display: block;
		width: 160px;
		height: 36px;
		margin-top: 6px;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
	}
</style>
