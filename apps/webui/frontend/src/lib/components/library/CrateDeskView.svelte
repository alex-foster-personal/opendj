<script lang="ts">
	// Props-only half of CrateDesk: the Library workspace grid. Every region
	// is a container (stores, engine, backend), so CrateDesk passes each one
	// in as a snippet and this file owns only the layout.
	import type { Snippet } from 'svelte';

	let {
		topbar,
		preview,
		browser,
		inspector
	}: { topbar: Snippet; preview: Snippet; browser: Snippet; inspector: Snippet } = $props();
</script>

<div class="perf-root library-root" data-testid="library-workspace">
	{@render topbar()}
	{@render preview()}
	<div class="desk">
		{@render browser()}
		{@render inspector()}
	</div>
</div>

<style>
	.library-root {
		position: fixed;
		inset: 0;
		display: grid;
		grid-template-areas:
			'topbar'
			'preview'
			'desk';
		grid-template-rows: var(--rb-topbar-h) 48px minmax(0, 1fr);
		background: var(--rb-bg);
	}
	.desk {
		grid-area: desk;
		display: grid;
		grid-template-areas: 'browser inspector';
		grid-template-columns: minmax(0, 1fr) 300px;
		min-height: 0;
	}
</style>
