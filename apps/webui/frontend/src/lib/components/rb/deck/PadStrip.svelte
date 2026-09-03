<script lang="ts">
	// Decorative A-H pad letter strip (COMPONENT-MAP): purely visual, carries
	// no control and no state, and is hidden by default because theme.css sets
	// --rb-pad-strip-display: none on .perf-root.
	//
	// Extracted from Deck.svelte rather than left inline: that file sits on the
	// quality ratchet's `file_size.over_limit_frontend` boundary with single
	// digits of headroom, so the deck's one block with no logic in it is the
	// honest thing to move out when the deck grows a real control. Nothing here
	// is agent-drivable, so it needs no performance-bus command.
	const PAD_LETTERS: string[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
</script>

<div class="pad-strip" aria-hidden="true">
	{#each PAD_LETTERS as letter, i (letter)}
		{#if i === 4}
			<span class="pad-sep">|</span>
		{/if}
		<span class="pad-letter">{letter}</span>
	{/each}
</div>

<style>
	.pad-strip {
		/* Toggle: set --rb-pad-strip-display: none on .perf-root to hide. */
		display: var(--rb-pad-strip-display, flex);
		align-items: center;
		justify-content: center;
		gap: 10px;
		font-size: var(--rb-fs-label);
		color: var(--rb-text-dim);
		line-height: 1;
		flex: 0 0 auto;
	}
	.pad-sep {
		color: var(--rb-border);
	}
</style>
