<script lang="ts">
	// Browser Preview column: lazy 400-point PWAV overview on a canvas
	// (COMPONENT-MAP 1.5 'Preview mini waveform column'). States:
	//   null          -> not fetched yet (parent fetches on row visibility)
	//   'unavailable' -> /anlz said no analysis: explicit tiny dash, never invented
	//   bands         -> tri-band render (mono kind = single colour, no synthesis)
	import type { AnlzWaveformBands } from '$lib/rb/types';

	// Canvas cannot read CSS custom properties cheaply; these literals mirror
	// theme.css --rb-orange / --rb-wave-mid / --rb-wave-high exactly.
	const COL_LOW = '#e8a13a';
	const COL_MID = '#3d7dd9';
	const COL_HIGH = '#cfe0f2';
	const W = 110;
	const H = 14;

	let {
		preview,
		kind
	}: {
		preview: AnlzWaveformBands | 'unavailable' | null;
		kind: 'tri' | 'mono' | null;
	} = $props();

	let canvas: HTMLCanvasElement | undefined = $state(undefined);

	$effect(() => {
		if (canvas !== undefined && preview !== null && preview !== 'unavailable') {
			_draw(canvas, preview, kind);
		}
	});

	function _draw(el: HTMLCanvasElement, bands: AnlzWaveformBands, k: 'tri' | 'mono' | null): void {
		const dpr = window.devicePixelRatio || 1;
		el.width = W * dpr;
		el.height = H * dpr;
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('PreviewStrip: 2d canvas context unavailable');
		ctx.scale(dpr, dpr);
		ctx.clearRect(0, 0, W, H);
		const n = bands.length;
		if (n === 0) return; // real empty-analysis payload: blank strip, no padding
		for (let x = 0; x < W; x++) {
			const i = Math.min(n - 1, Math.floor((x / W) * n));
			if (k === 'mono') {
				// mono kind = heights only; single colour, NEVER synthesised bands
				_bar(ctx, x, Math.max(bands.low[i], bands.mid[i], bands.high[i]), COL_MID);
			} else {
				_bar(ctx, x, bands.low[i], COL_LOW);
				_bar(ctx, x, bands.mid[i], COL_MID);
				_bar(ctx, x, bands.high[i], COL_HIGH);
			}
		}
	}

	function _bar(ctx: CanvasRenderingContext2D, x: number, v: number, colour: string): void {
		const h = Math.max(0, Math.min(1, v)) * H;
		if (h <= 0) return;
		ctx.fillStyle = colour;
		ctx.fillRect(x, H - h, 1, h);
	}
</script>

{#if preview === 'unavailable'}
	<span class="no-anlz" title="no analysis (ANLZ) data for this track">-</span>
{:else if preview === null}
	<span class="pending" aria-hidden="true"></span>
{:else}
	<canvas bind:this={canvas} style={`width:${W}px;height:${H}px`}></canvas>
{/if}

<style>
	canvas {
		display: block;
	}
	.no-anlz {
		display: inline-block;
		width: 110px;
		color: var(--rb-text-dim);
		text-align: center;
		line-height: 14px;
	}
	.pending {
		display: inline-block;
		width: 110px;
		height: 14px;
	}
</style>
