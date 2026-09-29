<script lang="ts">
	// Renders one lyric verdict mark (lyric-column.ts): an inline SVG icon for
	// the vocal verdict, the compact text mark for every other verdict.
	import type { LyricVerdictMark } from './lyric-column';

	let { mark }: { mark: LyricVerdictMark } = $props();
</script>

{#if mark.kind === 'icon'}
	<svg class="lyr-mark-icon" viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
		<path d={mark.path} fill="none" stroke="currentColor" stroke-width="2" />
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
