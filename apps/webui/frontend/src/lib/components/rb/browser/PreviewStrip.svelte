<script lang="ts">
	// Browser Preview column strip - INLINE data from the hydrated listing
	// payload (SPIKE-A1/A2 verdicts): 120-col tri-band uint8 strip decoded
	// once in BrowserPanel, drawn here per-row on a canvas.
	// Draw policy (SPIKE-A2): draw ONCE when the row first scrolls into
	// view (revealed flips true via the table's IntersectionObserver);
	// redraw only on DPR change or vocal-region arrival.
	// States (never invented):
	//   strip null  -> explicit dash: no ANLZ preview after the full
	//                  PWV6 -> PWV4 -> PWAV fallback chain
	//   vocals      -> SPIKE-B1/B2 four mandatory states: 'rekordbox' and
	//                  'demucs' (local detection) draw blue top bars;
	//                  'no_vocals' / 'not_analyzed' draw nothing and
	//                  tooltip the reason; null = unknown client-side,
	//                  so NO bars and NO claim either way
	import type { PreviewStripData, Vocals } from '$lib/rb/api-rb';
	import { VOCAL_BAR_PX, VOCAL_BLUE, vocalAlpha } from '../wave/render';

	// rekordbox's OWN 3Band Preview palette: blue bass / amber mid / white
	// highs (SPIKE-A1 section 3) - intentionally NOT the detail-waveform
	// palette. Literals: canvas cannot read CSS vars cheaply.
	const COL_LOW = '#3d7dd9';
	const COL_MID = '#e8a13a';
	const COL_HIGH = '#cfe0f2';
	const W = 110;
	const H = 14;

	let {
		strip,
		vocals,
		duration_ms,
		revealed
	}: {
		strip: PreviewStripData | null;
		vocals: Vocals | null;
		duration_ms: number | null;
		revealed: boolean;
	} = $props();

	let canvas: HTMLCanvasElement | undefined = $state(undefined);
	let dpr = $state(1);

	const title: string | null = $derived.by(() => {
		if (vocals === null) return null; // unknown client-side: say nothing
		if (vocals.status === 'no_vocals') return 'no vocals detected';
		else if (vocals.status === 'not_analyzed') return 'vocals not analyzed in rekordbox';
		else if (vocals.status === 'demucs') return 'vocals: local detection';
		else return null;
	});

	// Track devicePixelRatio so zoom/monitor moves trigger the one allowed
	// redraw cause besides vocal arrival (SPIKE-A2 contract).
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
		if (canvas !== undefined && strip !== null && revealed) {
			_draw(canvas, strip, vocals, duration_ms, dpr);
		}
	});

	// ----------------------------------------------------------- _helpers

	function _bandMax(bands: Uint8Array, band: 0 | 1 | 2, c0: number, c1: number): number {
		let max = 0;
		for (let c = c0; c <= c1; c++) {
			const v = bands[c * 3 + band];
			if (v > max) max = v;
		}
		return max;
	}

	function _draw(
		el: HTMLCanvasElement,
		data: PreviewStripData,
		voc: Vocals | null,
		durMs: number | null,
		ratio: number
	): void {
		el.width = W * ratio;
		el.height = H * ratio;
		const ctx = el.getContext('2d');
		if (ctx === null) throw new Error('PreviewStrip: 2d canvas context unavailable');
		ctx.scale(ratio, ratio);
		ctx.clearRect(0, 0, W, H);
		if (data.max > 0) {
			// Clamp-normalise by the SERVER-computed per-track max - never
			// /127 (A1 gotcha 3: observed values top out ~87). max == 0 with
			// all-zero bands is a real silent-analysis strip: nothing drawn.
			for (let x = 0; x < W; x++) {
				// 120 cols -> 110 px: peak-max per pixel bucket so transients
				// survive, mirroring the server's own downsample choice.
				const c0 = Math.floor((x / W) * data.cols);
				const c1 = Math.min(data.cols - 1, Math.max(c0, Math.ceil(((x + 1) / W) * data.cols) - 1));
				_bar(ctx, x, _bandMax(data.bands, 0, c0, c1) / data.max, COL_LOW);
				_bar(ctx, x, _bandMax(data.bands, 1, c0, c1) / data.max, COL_MID);
				_bar(ctx, x, _bandMax(data.bands, 2, c0, c1) / data.max, COL_HIGH);
			}
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
				ctx.fillRect(x0, 0, x1 - x0, VOCAL_BAR_PX);
			}
			ctx.globalAlpha = 1;
		}
	}

	function _bar(ctx: CanvasRenderingContext2D, x: number, v: number, colour: string): void {
		const h = Math.max(0, Math.min(1, v)) * H;
		if (h <= 0) return;
		ctx.fillStyle = colour;
		ctx.fillRect(x, H - h, 1, h);
	}
</script>

{#if strip === null}
	<span class="no-anlz" title="no preview data (no ANLZ waveform for this track)">-</span>
{:else}
	<canvas
		bind:this={canvas}
		title={title ?? undefined}
		style={`width:${W}px;height:${H}px`}
	></canvas>
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
</style>
