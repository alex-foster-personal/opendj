<script lang="ts">
	import type { WaveformDesign } from '$lib/rb/waveform-design';
	import { uiPrefs } from '$lib/rb/prefs.svelte';

	let { design }: { design?: WaveformDesign } = $props();

	const active = $derived(design ?? uiPrefs.waveform_design);

	let canvas: HTMLCanvasElement | undefined = $state();

	$effect(() => {
		const el = canvas;
		if (el === undefined) return;
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
			ctx.strokeStyle = '#3d7dd9';
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
			if (active === 'mono') {
				const bh = v * (h - 2);
				ctx.fillStyle = '#3d7dd9';
				ctx.fillRect(x, h - bh, barW, bh);
			} else {
				ctx.fillStyle = '#e8a13a';
				ctx.fillRect(x, h - v * (h - 2) * 0.55, barW, v * (h - 2) * 0.55);
				ctx.fillStyle = 'rgba(61, 125, 217, 0.85)';
				ctx.fillRect(x, h - v * (h - 2) * 0.75, barW, v * (h - 2) * 0.35);
				ctx.fillStyle = 'rgba(207, 224, 242, 0.9)';
				ctx.fillRect(x, h - v * (h - 2) * 0.45, barW, v * (h - 2) * 0.25);
			}
		}
	});
</script>

<canvas bind:this={canvas} class="wfd-preview" width="160" height="36" aria-label="Waveform design preview"></canvas>

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
