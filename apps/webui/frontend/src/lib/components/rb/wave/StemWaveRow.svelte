<script lang="ts">
	import type { DeckState } from '$lib/rb/deck-state-types';
	import type { StemControl } from '$lib/rb/stem-types';
	import {
		ensureStemWaveform,
		getStemWaveformEntry
	} from './stem-waveform-cache.svelte';
	import {
		stemWaveformApiPart,
		stemWaveRowColor,
		stemWaveRowUnavailableTip,
		STEM_WAVE_ROW_PX
	} from './stem-waveform-ui';
	import { drawStemWaveRow } from './render';

	let {
		deck,
		stem,
		showStems,
		scrollPx,
		canvasWidth
	}: {
		deck: DeckState;
		stem: StemControl;
		showStems: boolean;
		scrollPx: number;
		canvasWidth: number;
	} = $props();

	const unavailableTip = $derived(stemWaveRowUnavailableTip(deck, stem));
	const apiPart = $derived(
		deck.stems.status === 'ready' ? stemWaveformApiPart(stem, deck.stems.layout) : null
	);
	const shouldFetch = $derived(
		showStems &&
			deck.stable_id !== null &&
			unavailableTip === null &&
			apiPart !== null
	);

	$effect(() => {
		const sid = deck.stable_id;
		const part = apiPart;
		if (!shouldFetch || sid === null || part === null) return;
		ensureStemWaveform(sid, part);
	});

	const entry = $derived.by(() => {
		const sid = deck.stable_id;
		const part = apiPart;
		if (sid === null || part === null) return undefined;
		return getStemWaveformEntry(sid, part);
	});

	let canvasEl: HTMLCanvasElement | null = $state(null);

	$effect(() => {
		if (!shouldFetch || canvasEl === null || canvasWidth <= 0) return;
		const envelope = entry?.status === 'ready' ? entry.envelope : null;
		if (envelope === null) return;
		const dpr = window.devicePixelRatio || 1;
		const cssH = STEM_WAVE_ROW_PX;
		const pxW = Math.max(1, Math.round(canvasWidth * dpr));
		const pxH = Math.max(1, Math.round(cssH * dpr));
		if (canvasEl.width !== pxW || canvasEl.height !== pxH) {
			canvasEl.width = pxW;
			canvasEl.height = pxH;
		}
		const ctx = canvasEl.getContext('2d');
		if (ctx === null) return;
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		drawStemWaveRow(ctx, {
			envelope,
			scrollPx,
			durationMs: deck.duration_ms,
			pitch: deck.pitch,
			width: canvasWidth,
			height: cssH,
			color: stemWaveRowColor(stem)
		});
	});
</script>

{#if showStems && deck.stable_id !== null}
	<div
		class="stem-wave-row"
		class:inert={unavailableTip !== null}
		data-stem={stem}
		data-unavailable={unavailableTip !== null}
		aria-disabled={unavailableTip !== null}
		title={unavailableTip ?? `${stem} stem mini-waveform`}
	>
		{#if unavailableTip !== null}
			<span class="stem-wave-placeholder">{stem.toUpperCase()}</span>
		{:else}
			<canvas bind:this={canvasEl} aria-hidden="true"></canvas>
		{/if}
	</div>
{/if}

<style>
	.stem-wave-row {
		height: var(--rb-stemwave-h, 12px);
		position: relative;
		background: color-mix(in srgb, var(--rb-bg) 92%, transparent);
		border-top: 1px solid color-mix(in srgb, var(--rb-border) 60%, transparent);
	}
	.stem-wave-row.inert {
		opacity: 0.45;
		pointer-events: none;
	}
	.stem-wave-placeholder {
		display: block;
		padding: 0 6px;
		font-size: 8px;
		line-height: var(--rb-stemwave-h, 12px);
		color: var(--rb-text-dim);
		letter-spacing: 0.08em;
	}
	canvas {
		display: block;
		width: 100%;
		height: var(--rb-stemwave-h, 12px);
	}
</style>
