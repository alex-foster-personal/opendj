<script lang="ts">
	import TrackifyTopBar from '$lib/components/rb/TrackifyTopBar.svelte';

	let { children } = $props();
</script>

<div class="trackify-root">
	<TrackifyTopBar />
	<main>{@render children()}</main>
</div>

<style>
	.trackify-root {
		min-height: 100vh;
		display: grid;
		grid-template-rows: auto 1fr;
		background: var(--bg, #0b0b0f);
		color: var(--text, #f5f5f5);
	}
</style>
