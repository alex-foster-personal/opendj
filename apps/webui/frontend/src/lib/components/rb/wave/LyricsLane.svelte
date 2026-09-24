<script lang="ts">
	import { WAVE_WINDOW_S } from './render';
	import {
		activeLyricLineIndex,
		lyricLaneGroups,
		lyricLanePositionPercent,
		type LyricLine
	} from './lyrics-lane';

	const {
		lyrics,
		loadError,
		positionMs,
		pitch
	}: {
		lyrics: { lines: LyricLine[] } | null;
		loadError: Error | null;
		positionMs: number;
		pitch: number;
	} = $props();

	const activeIndex = $derived(
		lyrics === null ? -1 : activeLyricLineIndex(lyrics.lines, positionMs)
	);
	const groups = $derived(lyrics === null ? [] : lyricLaneGroups(lyrics.lines));
</script>

{#if loadError !== null}
	<span class="lyrics-error" title={loadError.message}>LYRICS ERROR</span>
{:else if lyrics !== null}
	<div class="lyrics-lane" aria-label="Synced lyrics">
		{#each groups as group (group.start_ms)}
			{@const active = activeIndex >= group.firstIndex && activeIndex <= group.lastIndex}
			{@const left = lyricLanePositionPercent({
				lineStartMs: group.start_ms,
				positionMs,
				pitch,
				windowSeconds: WAVE_WINDOW_S
			})}
			{#if left >= -10 && left <= 110}
				<span
					class:active
					class="lyric-line"
					style:left={`${left}%`}
					aria-current={active ? 'true' : undefined}
				>{group.text}</span>
			{/if}
		{/each}
	</div>
{/if}

<style>
	.lyrics-lane {
		position: absolute;
		inset: auto 0 2px;
		height: 16px;
		overflow: hidden;
		pointer-events: none;
		z-index: 2;
	}
	.lyric-line {
		position: absolute;
		bottom: 0;
		transform: translateX(-50%);
		white-space: nowrap;
		color: color-mix(in srgb, var(--rb-text) 70%, transparent);
		font-size: 10px;
		line-height: 16px;
		text-shadow: 0 1px 2px var(--rb-bg);
		transition: color 80ms linear, font-weight 80ms linear;
	}
	.lyric-line.active {
		color: var(--rb-text);
		font-weight: 700;
	}
	.lyrics-error {
		position: absolute;
		right: 4px;
		bottom: 2px;
		z-index: 3;
		color: #ff7b72;
		font-size: 9px;
		letter-spacing: 0.06em;
		pointer-events: auto;
	}
</style>
