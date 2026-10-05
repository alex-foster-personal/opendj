<script lang="ts">
	import { WAVE_WINDOW_S } from './render';
	import {
		activeLyricLineIndex,
		lyricLaneGroups,
		lyricLanePositionPercent,
		lyricLaneSlots,
		LYRIC_ENTRY_GAP_PX,
		LYRIC_ROW_CENTER_PCT,
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
	const slots = $derived(lyricLaneSlots(groups, pitch, WAVE_WINDOW_S));
</script>

{#if loadError !== null}
	<span class="lyrics-error" title={loadError.message}>LYRICS ERROR</span>
{:else if lyrics !== null}
	<div class="lyrics-lane" aria-label="Synced lyrics">
		{#each groups as group, index (group.start_ms)}
			{@const slot = slots[index]}
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
					style:top={`${LYRIC_ROW_CENTER_PCT[slot.row]}%`}
					style:max-width={Number.isFinite(slot.maxWidthPct)
						? `calc(${slot.maxWidthPct}% - ${LYRIC_ENTRY_GAP_PX}px)`
						: undefined}
					data-row={slot.row}
					aria-current={active ? 'true' : undefined}
				>{group.text}</span>
			{/if}
		{/each}
	</div>
{/if}

<style>
	.lyrics-lane {
		position: absolute;
		inset: 0;
		overflow: hidden;
		pointer-events: none;
		z-index: 2;
	}
	/* Left edge sits on the line's timestamp; max-width stops it short of the
	   next entry on its row (lyricLaneSlots), so rows never collide. Box and
	   outline are skin tokens (--rb-lyric-box-alpha, --rb-lyric-outline-px). */
	.lyric-line {
		position: absolute;
		transform: translateY(-50%);
		box-sizing: border-box;
		padding: 0 2px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: color-mix(in srgb, var(--rb-text) 85%, transparent);
		background: rgb(0 0 0 / var(--rb-lyric-box-alpha));
		font-size: 10px;
		line-height: 13px;
		text-shadow:
			var(--rb-lyric-outline-px) 0 #000,
			calc(-1 * var(--rb-lyric-outline-px)) 0 #000,
			0 var(--rb-lyric-outline-px) #000,
			0 calc(-1 * var(--rb-lyric-outline-px)) #000;
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
