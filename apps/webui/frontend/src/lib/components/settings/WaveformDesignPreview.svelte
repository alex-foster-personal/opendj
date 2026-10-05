<script lang="ts">
	import type { WaveformDesign } from '$lib/rb/waveform-design';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { resolveStripBandColors } from '$lib/rb/wave-palette';
	import { effectiveWaveformDesign, effectiveWavePalette } from '$lib/rb/ui-skin';

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
		const c = colors;
		const ctx = el.getContext('2d');
		if (ctx === null) return;
		const w = el.width;
		const h = el.height;
		ctx.clearRect(0, 0, w, h);
		ctx.fillStyle = '#0a0c10';
		ctx.fillRect(0, 0, w, h);
		const n = 48;
		const sample = Array.from({ length: n }, (_, i) => {
			const t = i / (n - 1);
			return 0.15 + 0.75 * Math.abs(Math.sin(t * Math.PI * 3)) * (1 - t * 0.2);
		});
		const barW = w / n;
		if (active === 'line') {
			ctx.strokeStyle = c.mono;
			ctx.lineWidth = 1.5;
			ctx.beginPath();
			for (let i = 0; i < n; i++) {
				const x = i * barW + barW / 2;
				const y = h - sample[i] * (h - 4);
				if (i === 0) ctx.moveTo(x, y);
				else ctx.lineTo(x, y);
			}
			ctx.stroke();
			return;
		}
		for (let i = 0; i < n; i++) {
			const x = i * barW;
			const v = sample[i];
			if (active === 'blocks') {
				// Mirrored single-color bars with a 1px gap (waveform-blocks-design).
				const bh = v * (h - 2);
				ctx.fillStyle = c.mono;
				ctx.fillRect(x, (h - bh) / 2, Math.max(1, barW - 1), bh);
			} else if (active === 'mono') {
				const bh = v * (h - 2);
				ctx.fillStyle = c.mono;
				ctx.fillRect(x, h - bh, barW, bh);
			} else {
				ctx.fillStyle = c.low;
				ctx.fillRect(x, h - v * (h - 2) * 0.55, barW, v * (h - 2) * 0.55);
				ctx.fillStyle = c.mid;
				ctx.fillRect(x, h - v * (h - 2) * 0.75, barW, v * (h - 2) * 0.35);
				ctx.fillStyle = c.high;
				ctx.fillRect(x, h - v * (h - 2) * 0.45, barW, v * (h - 2) * 0.25);
			}
		}
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
	title={`Preview: ${active} design, ${palette} colors (the effective look for the current skin)`}
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
