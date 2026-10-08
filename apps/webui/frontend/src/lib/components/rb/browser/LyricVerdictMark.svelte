<script lang="ts">
	// Renders one lyric verdict mark (lyric-column.ts): an inline SVG icon for
	// the vocal verdict, a lyric sheet with a small pending dot for lyrics that
	// are found but not yet checked (LYR-13), and the compact text mark for
	// every other verdict. Every part draws in currentColor, so the mark has
	// exactly the contrast of the row text in every skin.
	import type { LyricVerdictMark } from './lyric-column';

	let { mark }: { mark: LyricVerdictMark } = $props();
</script>

{#if mark.kind === 'icon'}
	<svg class="lyr-mark-icon" viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
		<path d={mark.path} fill="none" stroke="currentColor" stroke-width="2" />
	</svg>
{:else if mark.kind === 'pending'}
	<svg
		class="lyr-mark-icon lyr-mark-pending"
		viewBox="0 0 30 24"
		width="14"
		height="11"
		aria-hidden="true"
	>
		<path d={mark.path} fill="none" stroke="currentColor" stroke-width="2.4" />
		<circle class="lyr-pending-dot" cx="26" cy="5" r="3.2" fill="currentColor" />
	</svg>
{:else if mark.kind === 'text'}
	{mark.text}
{/if}

<style>
	.lyr-mark-icon {
		display: inline-block;
		vertical-align: -1px;
	}
</style>
