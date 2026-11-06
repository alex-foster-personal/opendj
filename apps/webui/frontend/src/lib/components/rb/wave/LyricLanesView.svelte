<!--
	Props-only half of LyricLanes: the word lane wins when the container hands
	it words (pref on AND the track has aligned words), otherwise the line lane
	renders. LyricLanes.svelte owns the word-lane lifecycle; this file only
	switches between the two display projections.
-->
<script lang="ts">
	import type { KaraokeWord } from '$lib/api-karaoke';
	import type { LyricLine } from './lyrics-lane';
	import LyricsLane from './LyricsLane.svelte';
	import WordLane from './WordLane.svelte';

	const {
		wordsError,
		words,
		lyrics,
		loadError,
		positionMs,
		pitch,
		windowSeconds,
		playheadFrac
	}: {
		/** Word-fetch failure reason, or null. */
		wordsError: string | null;
		/** Aligned words to paint, or null to fall back to the line lane. */
		words: KaraokeWord[] | null;
		lyrics: { lines: LyricLine[] } | null;
		loadError: Error | null;
		positionMs: number;
		pitch: number;
		windowSeconds: number;
		playheadFrac: number;
	} = $props();
</script>

{#if wordsError !== null}
	<span class="words-error" title={wordsError}>WORDS ERROR</span>
{/if}
{#if words !== null}
	<WordLane {words} {positionMs} {pitch} {windowSeconds} {playheadFrac} />
{:else}
	<LyricsLane {lyrics} {loadError} {positionMs} {pitch} {windowSeconds} {playheadFrac} />
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
