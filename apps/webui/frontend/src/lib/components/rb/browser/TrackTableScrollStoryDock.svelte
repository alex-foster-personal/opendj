<script lang="ts">
	/** Storybook-only frame: fixed-height column so TrackTable gets a real scroll viewport. */
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();
</script>

<div class="dock" data-testid="track-table-scroll-dock">
	{@render children()}
</div>

<style>
	.dock {
		height: 220px;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
</style>
