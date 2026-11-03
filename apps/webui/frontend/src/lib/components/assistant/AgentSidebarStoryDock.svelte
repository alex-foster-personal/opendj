<script lang="ts">
	/** Storybook-only frame: docks the sidebar on the right of a dimmed app backdrop. */
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();
</script>

<div class="dock">{@render children()}</div>

<style>
	.dock {
		height: 100vh;
		display: flex;
		justify-content: flex-end;
		background: color-mix(in srgb, var(--bg) 55%, black);
	}
</style>
