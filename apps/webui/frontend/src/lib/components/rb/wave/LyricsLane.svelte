<script lang="ts">
	import { WAVE_WINDOW_S } from './render';
	import { activeLyricLineIndex, lyricLanePositionPercent, type LyricLine } from './lyrics-lane';

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
</script>

{#if loadError !== null}
	<span class="lyrics-error" title={loadError.message}>LYRICS ERROR</span>
{:else if lyrics !== null}
	<div class="lyrics-lane" aria-label="Synced lyrics">
		{#each lyrics.lines as line, index (line.start_ms)}
			{@const left = lyricLanePositionPercent({
				lineStartMs: line.start_ms,
				positionMs,
				pitch,
				windowSeconds: WAVE_WINDOW_S
			})}
			{#if left >= -10 && left <= 110}
				<span
					class:active={index === activeIndex}
					class="lyric-line"
					style:left={`${left}%`}
					aria-current={index === activeIndex ? 'true' : undefined}
				>{line.text}</span>
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
