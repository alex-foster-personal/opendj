<!--
	Lyric overlay switch for one wave row. The word lane and main's line lane
	COEXIST: word lanes win when the pref is on AND the loaded track actually
	has aligned words, otherwise the line lane renders exactly as it does
	today. Nothing here decides geometry or fetches - word-lane-state.svelte
	owns the lifecycle, and both lanes are pure display projections.
-->
<script lang="ts">
	import type { LyricLine } from './lyrics-lane';
	import LyricsLane from './LyricsLane.svelte';
	import WordLane from './WordLane.svelte';
	import { createWordLaneState } from './word-lane-state.svelte';

	const {
		stableId,
		lyrics,
		loadError,
		positionMs,
		pitch
	}: {
		stableId: string | null;
		lyrics: { lines: LyricLine[] } | null;
		loadError: Error | null;
		positionMs: number;
		pitch: number;
	} = $props();

	const lane = createWordLaneState(() => stableId);
</script>

{#if lane.error !== null}
	<span class="words-error" title={lane.error}>WORDS ERROR</span>
{/if}
{#if lane.on && lane.words !== null}
	<WordLane words={lane.words} {positionMs} {pitch} />
{:else}
	<LyricsLane {lyrics} {loadError} {positionMs} {pitch} />
{/if}

<style>
	/* Mirrors LyricsLane's .lyrics-error, one row higher so a word-fetch
	   failure and a line-fetch failure can never overlap. */
	.words-error {
		position: absolute;
		right: 4px;
		bottom: 14px;
		z-index: 3;
		color: #ff7b72;
		font-size: 9px;
		letter-spacing: 0.06em;
		pointer-events: auto;
	}
</style>
