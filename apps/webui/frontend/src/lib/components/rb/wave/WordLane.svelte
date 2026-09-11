<script lang="ts">
	/**
	 * DOM overlay sibling of LyricsLane: per-word lanes packed by word-lanes.ts.
	 * Uses the presented transport clock (positionMs) like LyricsLane; does not
	 * import api-rb.
	 */
	import type { LyricWord } from '$lib/api';
	import { WAVE_WINDOW_S } from './render';
	import {
		activeLaneWordIdx,
		assignLanes,
		bucketPxPerS,
		lyricLaneYs,
		LANE_FONT_PX,
		sliceLanes,
		type AssignedLanes
	} from './word-lanes';

	const SUSPECT_WITNESS = new Set(['contradict', 'lost']);

	const {
		words,
		positionMs,
		pitch
	}: {
		words: LyricWord[];
		positionMs: number;
		pitch: number;
	} = $props();

	let rootEl = $state<HTMLDivElement | null>(null);
	let widthCss = $state(0);
	let heightCss = $state(0);

	$effect(() => {
		const node = rootEl;
		if (node === null || typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(() => {
			widthCss = node.clientWidth;
			heightCss = node.clientHeight;
		});
		ro.observe(node);
		widthCss = node.clientWidth;
		heightCss = node.clientHeight;
		return () => ro.disconnect();
	});

	const trackWindowS = $derived(WAVE_WINDOW_S * pitch);
	const pxPerSec = $derived(widthCss > 0 ? widthCss / trackWindowS : 0);
	const tLeftSec = $derived(Math.max(0, positionMs / 1000 - trackWindowS / 2));
	const positionS = $derived(positionMs / 1000);

	let assigned = $state<AssignedLanes | null>(null);
	let assignedKey = $state('');

	$effect(() => {
		if (pxPerSec <= 0 || words.length === 0) {
			assigned = null;
			assignedKey = '';
			return;
		}
		const bucket = bucketPxPerS(pxPerSec);
		const key = `${words.length}:${bucket}`;
		if (key === assignedKey && assigned !== null) return;
		assigned = assignLanes(words, bucket);
		assignedKey = key;
	});

	const packed = $derived(
		assigned === null || pxPerSec <= 0 || widthCss <= 0
			? []
			: sliceLanes(assigned, tLeftSec, pxPerSec, widthCss)
	);
	const activeIdx = $derived(
		assigned === null ? null : activeLaneWordIdx(assigned.laneWords, positionS)
	);
	const laneYs = $derived(heightCss > 0 ? lyricLaneYs(heightCss) : [0, 0]);
</script>

<div class="word-lane" bind:this={rootEl} aria-label="Synced word lyrics">
	{#each packed as p (p.word.idx)}
		{@const isActive = activeIdx !== null && p.word.idx === activeIdx}
		{@const witness = p.word.witness}
		{@const suspect = witness !== null && witness !== undefined && SUSPECT_WITNESS.has(witness)}
		<span
			class="word-label"
			class:active={isActive}
			class:suspect={suspect}
			style:left="{p.x}px"
			style:top="{laneYs[p.lane]}px"
			style:font-size="{LANE_FONT_PX}px"
			style:max-width="{p.widthPx}px"
		>{p.word.word}</span>
	{/each}
</div>

<style>
	.word-lane {
		position: absolute;
		inset: 0;
		overflow: hidden;
		pointer-events: none;
		z-index: 2;
	}
	.word-label {
		position: absolute;
		transform: translateY(-100%);
		white-space: nowrap;
		overflow: hidden;
		padding: 0 2px;
		border-radius: 2px;
		background: rgba(8, 10, 13, 0.78);
		color: rgba(230, 235, 242, 0.92);
		font-weight: 600;
		line-height: 1;
		text-shadow: 0 1px 2px var(--rb-bg);
	}
	.word-label.active {
		color: #ffffff;
		font-weight: 700;
	}
	.word-label.suspect {
		opacity: 0.72;
		text-decoration: underline;
		text-decoration-thickness: 1px;
		text-underline-offset: 2px;
	}
</style>
