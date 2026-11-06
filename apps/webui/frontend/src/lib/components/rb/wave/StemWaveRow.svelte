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
	import StemWaveRowView from './StemWaveRowView.svelte';

	let {
		deck,
		stem,
		showStems,
		scrollPx,
		canvasWidth,
		windowS,
		playheadFrac
	}: {
		deck: DeckState;
		stem: StemControl;
		showStems: boolean;
		scrollPx: number;
		canvasWidth: number;
		/** The main row's zoom window and playhead place (WAVEZOOM-6802/05). */
		windowS: number;
		playheadFrac: number;
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
			windowS,
			playheadFrac,
			durationMs: deck.duration_ms,
			pitch: deck.pitch,
			width: canvasWidth,
			height: cssH,
			color: stemWaveRowColor(stem, getComputedStyle(canvasEl).getPropertyValue(`--rb-stem-${stem}`))
		});
	});
</script>

<StemWaveRowView
	{stem}
	visible={showStems && deck.stable_id !== null}
	{unavailableTip}
	bind:canvasEl
/>
