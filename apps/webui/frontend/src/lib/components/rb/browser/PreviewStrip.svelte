<script lang="ts">
	// Browser Preview column strip - INLINE data from the hydrated listing
	// payload (SPIKE-A1/A2 verdicts): tri-band uint8 strip decoded once in
	// BrowserPanel, drawn here per-row on a canvas.
	// Draw policy (SPIKE-A2): draw ONCE when the row first scrolls into
	// view (revealed flips true via the table's IntersectionObserver);
	// redraw only on DPR change or vocal-region arrival.
	// Interactive: hover = thin red scrub line; click = onseek(ratio) if wired.
	import type { PreviewStripData, Vocals } from '$lib/rb/api-rb';
	import { VOCAL_BLUE, vocalAlpha } from '../wave/render';

	const COL_LOW = '#3d7dd9';
	const COL_MID = '#e8a13a';
	const COL_HIGH = '#cfe0f2';
	const W = 165;
	const H = 14;
	/** Mini-strip vocal overlay height (CSS px). */
	const VOCAL_BAR_H = 0.7;

	let {
		strip,
		vocals,
		duration_ms,
		revealed,
		onseek
	}: {
		strip: PreviewStripData | null;
		vocals: Vocals | null;
		duration_ms: number | null;
		revealed: boolean;
		onseek?: (ratio: number) => void;
	} = $props();

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
		if (canvas !== undefined && strip !== null && revealed) {
			_draw(canvas, strip, vocals, duration_ms, dpr);
		}
	});

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
		ctx.setTransform(1, 0, 0, 1, 0, 0);
		ctx.scale(ratio, ratio);
		ctx.clearRect(0, 0, W, H);
		if (data.max > 0) {
			for (let x = 0; x < W; x++) {
				const c0 = Math.floor((x / W) * data.cols);
				const c1 = Math.min(
					data.cols - 1,
					Math.max(c0, Math.ceil(((x + 1) / W) * data.cols) - 1)
				);
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
				ctx.fillRect(x0, 0, x1 - x0, VOCAL_BAR_H);
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

	function _ratioFromEvent(event: MouseEvent): number {
		const el = event.currentTarget as HTMLElement;
		const rect = el.getBoundingClientRect();
		return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
	}

	function onMove(event: MouseEvent): void {
		hoverX = _ratioFromEvent(event) * W;
	}

	function onLeave(): void {
		hoverX = null;
	}

	function onClick(event: MouseEvent): void {
		event.stopPropagation();
		onseek?.(_ratioFromEvent(event));
	}
</script>

{#if strip === null}
	<span class="no-anlz" title="no preview data (no ANLZ waveform for this track)">-</span>
{:else}
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="preview-hit"
		style={`width:${W}px;height:${H}px`}
		title={title ?? 'Click to seek if loaded · hover shows position'}
		onpointermove={onMove}
		onpointerleave={onLeave}
		onclick={onClick}
	>
		<canvas bind:this={canvas} style={`width:${W}px;height:${H}px`}></canvas>
		{#if hoverX !== null}
			<span class="scrub" style={`left:${hoverX}px`} aria-hidden="true"></span>
		{/if}
	</div>
{/if}

<style>
	.preview-hit {
		position: relative;
		display: inline-block;
		cursor: crosshair;
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
	.no-anlz {
		display: inline-block;
		width: 165px;
		color: var(--rb-text-dim);
		text-align: center;
		line-height: 14px;
	}
</style>
