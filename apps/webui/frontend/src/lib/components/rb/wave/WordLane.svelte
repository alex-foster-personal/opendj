<!--
	Word lane: the DOM half of the karaoke overlay, absolutely positioned
	over one deck waveform. Placement comes exclusively from word-lanes.ts
	(assignLanes + sliceLanes fed by laneWindow), so this component makes no
	geometry decisions of its own - it paints what the packer placed.

	MEMO STRATEGY (word-lanes.ts header contract): lane assignment depends
	only on word times, label widths and the px-per-second BUCKET, not on the
	window position, so it runs once per (words identity, bucketPxPerS) and
	each frame pays only sliceLanes (binary search + walk over visible
	words). The memo lives in plain per-instance locals on purpose: it is a
	paint-path cache, not UI state, so it must not be $state.
-->
<script lang="ts">
	import type { KaraokeWord } from '$lib/api-karaoke';
	import { WAVE_WINDOW_S } from './render';
	import {
		assignLanes,
		activeLaneWordIdx,
		bucketPxPerS,
		laneWindow,
		laneWordMaxWidthsPx,
		sliceLanes,
		LANE_FONT_PX,
		type AssignedLanes,
		type LaneInputWord,
		type PackedLaneWord
	} from './word-lanes';

	const {
		words,
		positionMs,
		pitch
	}: {
		words: KaraokeWord[];
		positionMs: number;
		pitch: number;
	} = $props();

	/** Per-lane text box height; two of them fill the 24px root. */
	const LANE_HEIGHT_PX = 12;
	/** Witness classes drawn as SUSPECT (render.ts drawLyricLanes contract):
	 * round-5 calibration showed contradict/lost carry 4.5x the base
	 * word-timing error rate. Shown dimmer and underlined, never hidden. */
	const SUSPECT_WITNESS: ReadonlySet<string> = new Set(['contradict', 'lost']);

	/** Measured lane width. 0 until the row is laid out - a hidden lane has
	 * no honest geometry, so nothing is painted at that width. */
	let widthCss = $state(0);

	let _sourceRef: KaraokeWord[] | null = null;
	let _laneInput: LaneInputWord[] = [];
	let _witnessByIdx = new Map<number, string | null>();
	let _laneBucket: number | null = null;
	let _assigned: AssignedLanes | null = null;

	/** Packer input for one payload: the served words carry optional times,
	 * the packer requires them present, and witness travels beside the lane
	 * words rather than through them (assignLanes is shape-fixed). */
	function _adoptWords(source: KaraokeWord[]): void {
		_sourceRef = source;
		_laneInput = source.map((word) => ({
			idx: word.idx,
			word: word.word,
			start_s: word.start_s ?? null,
			end_s: word.end_s ?? null
		}));
		_witnessByIdx = new Map(source.map((word) => [word.idx, word.witness ?? null]));
		_assigned = null;
	}

	function _assignedLanesFor(source: KaraokeWord[], pxPerS: number): AssignedLanes {
		if (source !== _sourceRef) _adoptWords(source);
		const bucket = bucketPxPerS(pxPerS);
		if (bucket !== _laneBucket || _assigned === null) {
			_assigned = assignLanes(_laneInput, bucket);
			_laneBucket = bucket;
		}
		return _assigned;
	}

	const frame = $derived.by((): {
		packed: PackedLaneWord[];
		maxWidths: (number | null)[];
		activeIdx: number | null;
	} | null => {
		if (widthCss <= 0) return null;
		const win = laneWindow({ positionMs, pitch, widthCss, windowSeconds: WAVE_WINDOW_S });
		const assigned = _assignedLanesFor(words, win.pxPerS);
		const packed = sliceLanes(assigned, win.tLeftSec, win.pxPerS, widthCss);
		return {
			packed,
			maxWidths: laneWordMaxWidthsPx(packed),
			activeIdx: activeLaneWordIdx(assigned.laneWords, positionMs / 1000)
		};
	});

	function _isSuspect(idx: number): boolean {
		const witness = _witnessByIdx.get(idx) ?? null;
		return witness !== null && SUSPECT_WITNESS.has(witness);
	}

	/** Hover title for a numeric readout (house rule): the sung onset the
	 * placement came from, plus the ASR witness verdict behind the styling. */
	function _wordTitle(word: LaneInputWord): string {
		const witness = _witnessByIdx.get(word.idx) ?? null;
		const onset = word.start_s === null ? 'unaligned' : `${word.start_s.toFixed(2)}s`;
		return `sung onset ${onset} - ASR witness ${witness ?? 'unjudged'}`;
	}
</script>

<div
	class="word-lane"
	aria-label="Karaoke words"
	style:font-size={`${LANE_FONT_PX}px`}
	bind:clientWidth={widthCss}
>
	{#if frame !== null}
		{#each frame.packed as packed, i (packed.word.idx)}
			{@const maxWidth = frame.maxWidths[i]}
			<span
				class="lane-word"
				class:suspect={_isSuspect(packed.word.idx)}
				class:active={packed.word.idx === frame.activeIdx}
				style:left={`${packed.x}px`}
				style:top={`${packed.lane * LANE_HEIGHT_PX}px`}
				style:max-width={maxWidth === null ? undefined : `${maxWidth}px`}
				title={_wordTitle(packed.word)}
				aria-current={packed.word.idx === frame.activeIdx ? 'true' : undefined}
			>{packed.word.word}</span>
		{/each}
	{/if}
</div>

<style>
	.word-lane {
		position: absolute;
		inset: auto 0 2px;
		height: 24px;
		overflow: hidden;
		pointer-events: none;
		z-index: 2;
	}
	.lane-word {
		position: absolute;
		height: 12px;
		line-height: 12px;
		white-space: nowrap;
		pointer-events: none;
		/* Same backing as the line lane (LyricsLane .lyric-line, pin 6b1da5a8).
		   The padding is pulled back by an equal negative margin so the first
		   glyph still sits on the sung onset. */
		box-sizing: border-box;
		margin-left: -2px;
		padding: 0 2px;
		overflow: hidden;
		text-overflow: ellipsis;
		border-radius: 2px;
		background: color-mix(in srgb, var(--rb-bg) 85%, transparent);
		color: color-mix(in srgb, var(--rb-text) 70%, transparent);
		text-shadow: 0 1px 2px var(--rb-bg);
		transition: color 80ms linear, font-weight 80ms linear;
	}
	/* Suspect first, active second: a suspect word at the playhead still
	   reads as the active one. */
	.lane-word.suspect {
		color: color-mix(in srgb, #ff7b72 70%, transparent);
		text-decoration: underline wavy;
	}
	.lane-word.active {
		color: var(--rb-text);
		font-weight: 700;
	}
</style>
