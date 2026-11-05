<script lang="ts">
	// Props-only half of CrateDesk: the Library workspace grid. Every region
	// is a container (stores, engine, backend), so CrateDesk passes each one
	// in as a snippet and this file owns only the layout.
	//
	// SKINLAYOUT-8869: `layout` is the skin manifest's library preset
	// (lib/rb/skin-layout.ts). Each preset is a grid-area map below over the
	// same regions; 'three-panel' adds the nowplaying and playingon regions.
	import type { Snippet } from 'svelte';
	import type { LibraryLayout } from '$lib/rb/skin-layout';

	let {
		layout = 'crate-desk',
		topbar,
		preview,
		browser,
		inspector,
		nowplaying,
		playingon
	}: {
		layout?: LibraryLayout;
		topbar: Snippet;
		preview: Snippet;
		browser: Snippet;
		inspector: Snippet;
		nowplaying?: Snippet;
		playingon?: Snippet;
	} = $props();
	const threePanel = $derived(
		layout === 'three-panel' && nowplaying !== undefined && playingon !== undefined
	);
</script>

<div
	class="perf-root library-root"
	data-testid="library-workspace"
	data-layout={threePanel ? 'three-panel' : 'crate-desk'}
>
	{@render topbar()}
	{#if threePanel}
		<div class="desk">
			{@render browser()}
			{@render nowplaying?.()}
		</div>
		<div class="player">
			{@render preview()}
			{@render playingon?.()}
		</div>
	{:else}
		{@render preview()}
		<div class="desk">
			{@render browser()}
			{@render inspector()}
		</div>
	{/if}
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
	/* Three-panel: Your Library (the browser's tree, left) and the track list
	 * (center) stay one BrowserPanel; Now Playing sits right at 320 px; the
	 * player strip runs along the bottom. Widths mimic the reference desktop
	 * layout: 8 px gutters, rounded panels. */
	.library-root[data-layout='three-panel'] {
		grid-template-areas:
			'topbar'
			'desk'
			'player';
		grid-template-rows: var(--rb-topbar-h) minmax(0, 1fr) auto;
	}
	.library-root[data-layout='three-panel'] .desk {
		grid-template-areas: 'browser nowplaying';
		grid-template-columns: minmax(0, 1fr) 320px;
		gap: 8px;
		padding: 8px 8px 0;
	}
	/* The browser's own regions take the panel look: rounded tree and list
	 * panels with a gutter, source tabs as pill chips. Scoped to this
	 * layout so Gig's browser and the other skins are untouched. */
	.library-root[data-layout='three-panel'] :global(.rb-browser .tree-panel),
	.library-root[data-layout='three-panel'] :global(.rb-browser .list-panel) {
		border-radius: 8px;
		background: var(--rb-panel);
		overflow: hidden;
	}
	.library-root[data-layout='three-panel'] :global(.view-tabs .vt) {
		border-radius: 999px;
		padding: 4px 12px;
		border: none;
		background: var(--rb-bg);
		color: var(--rb-text);
		font-weight: 600;
	}
	.library-root[data-layout='three-panel'] :global(.view-tabs .vt[aria-selected='true']) {
		background: var(--rb-text);
		color: var(--rb-bg);
	}
	.player {
		grid-area: player;
		display: grid;
		grid-template-areas: 'preview' 'playingon';
		grid-template-rows: 48px auto;
		padding-top: 8px;
	}
</style>
