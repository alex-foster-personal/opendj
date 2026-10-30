<script lang="ts">
	/**
	 * Two stacked deck waveforms for one try-er candidate with the knob lines the renderer
	 * applied (TRYER-16): outgoing on top, incoming below, each with its own beat grid (bar lines
	 * brighter than beats); fader, low EQ and filter drawn
	 * only while that deck plays. Click anywhere to play this candidate from that point.
	 * Data: GET /api/v1/dj/try/{job}/candidates/{cid}/lanes (CLI/MCP read the same route).
	 */
	import { getLanes, type TryLanes } from '$lib/api/dj-try';
	import { LANE_KNOBS, laneY, peakColumns, segmentsWithin } from './tryer-lanes';

	let {
		jobId,
		candidateId,
		playhead,
		onSeek
	}: {
		jobId: string;
		candidateId: string;
		/** Seconds into this candidate's render, or null when another candidate (or nothing) plays. */
		playhead: number | null;
		onSeek: (seconds: number) => void;
	} = $props();

	const ROW_PX = 72;
	let lanes = $state<TryLanes | null>(null);
	let failure = $state<string | null>(null);
	let width = $state(0);
	let canvas = $state<HTMLCanvasElement | null>(null);

	$effect(() => {
		const columns = 800;
		lanes = null;
		failure = null;
		getLanes(jobId, candidateId, columns).then(
			(body) => (lanes = body),
			(error: unknown) => (failure = error instanceof Error ? error.message : String(error))
		);
	});

	$effect(() => {
		if (canvas === null || lanes === null || width === 0) return;
		const scale = window.devicePixelRatio || 1;
		const height = ROW_PX * lanes.decks.length;
		canvas.width = Math.round(width * scale);
		canvas.height = Math.round(height * scale);
		const ctx = canvas.getContext('2d');
		if (ctx === null) throw new Error('no 2d canvas context');
		ctx.setTransform(scale, 0, 0, scale, 0, 0);
		ctx.clearRect(0, 0, width, height);
		const xOf = (seconds: number) => (seconds / lanes!.duration_s) * width;
		lanes.decks.forEach((deck, row) => {
			const top = row * ROW_PX;
			const mid = top + ROW_PX / 2;
			ctx.fillStyle = row === 0 ? 'rgba(90,160,255,0.55)' : 'rgba(255,150,70,0.55)';
			for (const column of peakColumns(deck.peaks, width)) {
				const half = column.peak * (ROW_PX / 2 - 2);
				ctx.fillRect(column.x, mid - half, Math.max(1, column.w), half * 2);
			}
			for (const [seconds, n] of deck.beats) {
				ctx.fillStyle = n === 1 ? 'rgba(255,255,255,0.45)' : 'rgba(255,255,255,0.14)';
				ctx.fillRect(Math.round(xOf(seconds)), top, 1, ROW_PX);
			}
			for (const knob of LANE_KNOBS) {
				ctx.strokeStyle = knob.color;
				ctx.setLineDash(knob.dash);
				ctx.lineWidth = 1.5;
				for (const segment of segmentsWithin(deck.curves[knob.name] ?? [], deck.playing)) {
					ctx.beginPath();
					segment.forEach(([t, v], i) => {
						const y = laneY(v, top, ROW_PX);
						if (i === 0) ctx.moveTo(xOf(t), y);
						else ctx.lineTo(xOf(t), y);
					});
					ctx.stroke();
				}
			}
			ctx.setLineDash([]);
		});
		if (playhead !== null) {
			ctx.strokeStyle = '#fff';
			ctx.lineWidth = 2;
			ctx.beginPath();
			ctx.moveTo(xOf(playhead), 0);
			ctx.lineTo(xOf(playhead), height);
			ctx.stroke();
		}
	});

	function seek(event: MouseEvent): void {
		if (lanes === null || width === 0) return;
		const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
		onSeek(((event.clientX - box.left) / box.width) * lanes.duration_s);
	}
</script>

<div class="lanes" bind:clientWidth={width}>
	{#if failure !== null}
		<p class="error" role="alert">lanes: {failure}</p>
	{:else if lanes === null}
		<p class="loading" role="status">loading waveforms...</p>
	{:else}
		<span class="label out" title="Outgoing deck">out</span>
		<span class="label in" title="Incoming deck" style:top="{ROW_PX}px">in</span>
		<canvas
			bind:this={canvas}
			style:width="{width}px"
			style:height="{ROW_PX * lanes.decks.length}px"
			onclick={seek}
			title="Click to play this transition from here ({lanes.duration_s.toFixed(1)} s render)"
		></canvas>
	{/if}
</div>

<style>
	.lanes {
		position: relative;
		width: 100%;
		min-height: 144px;
	}
	canvas {
		display: block;
		cursor: pointer;
		background: rgba(255, 255, 255, 0.03);
		border-radius: 4px;
	}
	.label {
		position: absolute;
		left: 4px;
		font-size: 0.7em;
		opacity: 0.7;
		pointer-events: none;
	}
	.error {
		color: var(--danger, #e44);
	}
	.loading {
		font-size: 0.8em;
		opacity: 0.7;
	}
</style>
