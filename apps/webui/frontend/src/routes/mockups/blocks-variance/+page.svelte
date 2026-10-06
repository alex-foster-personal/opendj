<script lang="ts">
	// Dev mockup: the four 'blocks' height variants stacked on ONE real track
	// at deck zoom (24s window), painted by the production drawWaveRow.
	// /mockups/blocks-variance?sid=<stable_id>&t=<seconds>&h=<row px>&dur=track|points
	// dur=track (default) uses the track's duration_ms exactly as the deck does;
	// dur=points uses detail.length / 150 (PWV7 is 150 points/s), which removes
	// the waveform-vs-grid drift a whole-second-rounded duration_ms causes.
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { fetchAnlz, fetchTrackBypassingHttpCache } from '$lib/rb/api-rb';
	import {
		adjacentBlockVariance,
		bandPeaksFor,
		beatPeriodS,
		blockHeights,
		BLOCK_PITCH_PX,
		drawWaveRow,
		readPalette,
		WAVE_WINDOW_S,
		type BlocksVariant
	} from '$lib/components/rb/wave/render';
	import '$lib/rb/theme.css';

	const VARIANTS: readonly BlocksVariant[] = ['max', 'blend', 'kick', 'contrast', 'onset'];
	const WIDTH = 1240;

	const sid = page.url.searchParams.get('sid');
	const tS = Number(page.url.searchParams.get('t') ?? '108');
	const rowH = Number(page.url.searchParams.get('h') ?? '90');
	const durMode = page.url.searchParams.get('dur') ?? 'track';
	const PWV7_POINTS_PER_S = 150;

	let root: HTMLDivElement;
	let canvases: HTMLCanvasElement[] = $state([]);
	let title = $state('');
	let scores: Record<string, string> = $state({});
	let paintMs: Record<string, string> = $state({});
	let error: string | null = $state(null);

	onMount(async () => {
		if (sid === null) {
			error = 'missing ?sid=<stable_id>';
			return;
		}
		const [anlz, track] = await Promise.all([fetchAnlz(sid), fetchTrackBypassingHttpCache(sid)]);
		if (track.duration_ms === null || track.duration_ms === undefined) throw new Error('track has no duration_ms');
		let durationMs: number;
		if (durMode === 'track') durationMs = track.duration_ms;
		else if (durMode === 'points') durationMs = (anlz.waveform.detail.length / PWV7_POINTS_PER_S) * 1000;
		else throw new Error(`dur must be track|points, got ${durMode}`);
		title = `${track.artist} - ${track.title} @ ${tS}s, duration ${durationMs.toFixed(0)}ms (${durMode})`;
		const palette = readPalette(root);
		const dpr = window.devicePixelRatio || 1;
		const pxPerS = WIDTH / WAVE_WINDOW_S;
		const period = beatPeriodS(anlz.beatgrid?.beats);
		const bpb = period === null ? null : (period * pxPerS) / BLOCK_PITCH_PX;
		const norms = bandPeaksFor(anlz.waveform);
		VARIANTS.forEach((variant, i) => {
			const el = canvases[i];
			el.width = WIDTH * dpr;
			el.height = rowH * dpr;
			const ctx = el.getContext('2d');
			if (ctx === null) throw new Error('2d context unavailable');
			ctx.scale(dpr, dpr);
			const frame = {
				widthCss: WIDTH,
				heightCss: rowH,
				positionMs: tS * 1000,
				durationMs,
				anlz,
				palette,
				pitch: 1,
				loop: null,
				waveformDesign: 'blocks' as const,
				blocksVariant: variant
			};
			const t0 = performance.now();
			drawWaveRow(ctx, frame); // cold: builds the cached band image
			const cold = performance.now() - t0;
			const t1 = performance.now();
			drawWaveRow(ctx, { ...frame, positionMs: tS * 1000 + 16 }); // warm: cache hit
			const warm = performance.now() - t1;
			paintMs[variant] = `cold ${cold.toFixed(1)}ms / warm ${warm.toFixed(2)}ms`;
			const visible = blockHeights(anlz.waveform.detail, Math.ceil((durationMs / 1000) * pxPerS), norms, variant, bpb);
			scores[variant] = adjacentBlockVariance(visible).toFixed(4);
		});
	});
</script>

<div class="perf-root mock" bind:this={root}>
	<h1>blocks variance {title}</h1>
	{#if error !== null}<p class="err">{error}</p>{/if}
	{#each VARIANTS as variant, i (variant)}
		<div class="row">
			<div class="label">
				<b>{variant}</b>
				<span title="Mean squared difference between adjacent block heights over the whole track; higher = individual beats read better">adjVar {scores[variant] ?? '...'}</span>
				<span title="drawWaveRow time: cold builds the cached band image, warm is a scrolled frame on the cache">{paintMs[variant] ?? ''}</span>
			</div>
			<canvas bind:this={canvases[i]} style="width:{WIDTH}px;height:{rowH}px"></canvas>
		</div>
	{/each}
</div>

<style>
	.mock {
		min-height: 100vh;
		padding: 12px 16px;
	}
	h1 {
		font-size: 12px;
		font-weight: 500;
		margin: 0 0 10px;
	}
	.row {
		margin-bottom: 10px;
		border: 1px solid var(--rb-border);
	}
	.label {
		display: flex;
		gap: 16px;
		padding: 3px 6px;
		color: var(--rb-text-dim);
		border-bottom: 1px solid var(--rb-border);
	}
	.label b {
		color: var(--rb-text);
	}
	canvas {
		display: block;
	}
	.err {
		color: var(--rb-red);
	}
</style>
