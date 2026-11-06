<!--
	Lyric overlay switch for one wave row. The word lane and main's line lane
	COEXIST: word lanes win when the pref is on AND the loaded track actually
	has aligned words, otherwise the line lane renders exactly as it does
	today. Nothing here decides geometry or fetches - word-lane-state.svelte
	owns the lifecycle, and both lanes are pure display projections. The
	markup lives in LyricLanesView.svelte so it can carry a story.
-->
<script lang="ts">
	import type { LyricLine } from './lyrics-lane';
	import LyricLanesView from './LyricLanesView.svelte';
	import { createWordLaneState } from './word-lane-state.svelte';

	const {
		stableId,
		lyrics,
		loadError,
		positionMs,
		pitch,
		windowSeconds,
		playheadFrac
	}: {
		stableId: string | null;
		lyrics: { lines: LyricLine[] } | null;
		loadError: Error | null;
		positionMs: number;
		pitch: number;
		/** The row's zoom window and playhead place (WAVEZOOM-6802/05). Required. */
		windowSeconds: number;
		playheadFrac: number;
	} = $props();

	const lane = createWordLaneState(() => stableId);
</script>

<LyricLanesView
	wordsError={lane.error}
	words={lane.on ? lane.words : null}
	{lyrics}
	{loadError}
	{positionMs}
	{pitch}
	{windowSeconds}
	{playheadFrac}
/>
