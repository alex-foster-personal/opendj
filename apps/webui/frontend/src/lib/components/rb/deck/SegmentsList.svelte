<script lang="ts">
	// STRUCT-9657: the Segments tab of the HOT CUE dropdown (PadModeMenu).
	// One row per section: label, start bar, length in bars, start time; the
	// row holding the playhead is highlighted. The parent owns the commands
	// (seek, hot_cue_save); this view only lays the rows out. Row model:
	// segments-list.ts.
	import type { AnlzBeat } from '$lib/rb/anlz-types';
	import type { HotCue, HotCueSlot } from '$lib/rb/hot-cue-types';
	import type { SectionSpan } from '$lib/components/rb/wave/sections';
	import {
		HOT_CUES_FULL_MESSAGE,
		NO_SECTIONS_MESSAGE,
		buildSegmentRows,
		firstEmptyHotCueSlot
	} from './segments-list';

	let {
		spans,
		beats,
		positionMs,
		hotCues,
		onSeek,
		onSaveHotCue
	}: {
		spans: readonly SectionSpan[];
		beats: readonly AnlzBeat[] | null;
		positionMs: number;
		hotCues: readonly Pick<HotCue, 'slot'>[];
		onSeek: (positionMs: number) => void;
		onSaveHotCue: (slot: HotCueSlot, positionMs: number) => void;
	} = $props();

	const model = $derived(buildSegmentRows(spans, beats, positionMs));
	let notice = $state<string | null>(null);

	function saveHotCue(startMs: number): void {
		const slot = firstEmptyHotCueSlot(hotCues);
		if (slot === null) {
			notice = HOT_CUES_FULL_MESSAGE;
			return;
		}
		notice = null;
		onSaveHotCue(slot, startMs);
	}
</script>

<div class="segments" data-testid="segments-list">
	{#if model.rows.length === 0}
		<p class="segments-empty">{NO_SECTIONS_MESSAGE}</p>
	{:else}
		{#each model.rows as row, i (i)}
			<div
				class="segment-row"
				class:playing={i === model.playing_index}
				data-playing={i === model.playing_index}
			>
				<button
					type="button"
					class="segment-jump"
					title={`Jump to ${row.label} at ${row.time}`}
					onclick={() => onSeek(row.start_ms)}
				>
					<span class="segment-label">{row.label}</span>
					<span class="segment-bar">{row.start_bar === null ? '-' : `bar ${row.start_bar}`}</span>
					<span class="segment-len"
						>{row.length_bars === null ? '-' : `${row.length_bars} bars`}</span
					>
					<span class="segment-time">{row.time}</span>
				</button>
				<button
					type="button"
					class="segment-cue"
					title={`Save ${row.label} as a hot cue in the first empty slot`}
					onclick={() => saveHotCue(row.start_ms)}>+ hot cue</button
				>
			</div>
		{/each}
		{#if notice !== null}
			<p class="segments-notice" role="status">{notice}</p>
		{/if}
	{/if}
</div>

<style>
	.segments {
		min-width: 260px;
		max-height: 240px;
		overflow-y: auto;
		font-size: 11px;
	}
	.segments-empty,
	.segments-notice {
		margin: 0;
		padding: 5px 10px;
		color: var(--rb-text-dim);
	}
	.segment-row {
		display: flex;
		align-items: center;
	}
	.segment-row.playing {
		background: rgba(255, 255, 255, 0.1);
	}
	.segment-jump {
		flex: 1;
		display: grid;
		grid-template-columns: 1fr 52px 52px 36px;
		gap: 4px;
		text-align: left;
		background: transparent;
		border: none;
		color: var(--rb-text);
		font-size: 11px;
		padding: 4px 10px;
		cursor: pointer;
	}
	.segment-jump:hover,
	.segment-cue:hover {
		background: rgba(255, 255, 255, 0.06);
	}
	.segment-bar,
	.segment-len,
	.segment-time {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.segment-cue {
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-size: 10px;
		padding: 4px 8px;
		cursor: pointer;
		white-space: nowrap;
	}
</style>
