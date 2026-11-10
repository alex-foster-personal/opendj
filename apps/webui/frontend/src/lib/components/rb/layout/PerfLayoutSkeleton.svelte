<script lang="ts">
	/**
	 * Layout skeleton for story-previews (#10264): the `.perf-root` grid for one
	 * performance preset from PERFORMANCE_LAYOUT_SPEC, with labelled placeholder
	 * boxes only. No engine, no live decks. Pixel-diffed by story-previews so a
	 * shape change on any preset is caught.
	 */
	import { PERFORMANCE_LAYOUT_SPEC, type PerformanceLayout } from '$lib/rb/skin-layout';
	import { TOPBAR_H_PX, WAVEROW_H_PX_TALLEST } from '$lib/rb/theme-tokens';

	let {
		preset,
		widthPx = 1280,
		heightPx = 800
	}: {
		preset: PerformanceLayout;
		widthPx?: number;
		heightPx?: number;
	} = $props();

	const spec = $derived(PERFORMANCE_LAYOUT_SPEC[preset]);
	const areasCss = $derived(spec.areas.map((row) => `"${row}"`).join(' '));
	const rowsCss = $derived(
		[
			`${TOPBAR_H_PX}px`,
			`${spec.floors.wavestackRows * WAVEROW_H_PX_TALLEST}px`,
			`${spec.floors.deckRowPx}px`,
			'minmax(0, 1fr)'
		].join(' ')
	);

	/** Region tokens named in `areas`, split on whitespace (one cell name each). */
	const areaNames = $derived([
		...new Set(spec.areas.flatMap((row) => row.trim().split(/\s+/).filter(Boolean)))
	]);
</script>

<div
	class="perf-root perf-layout-skeleton"
	data-layout={preset}
	data-testid="perf-layout-skeleton"
	style:width="{widthPx}px"
	style:height="{heightPx}px"
	style:grid-template-areas={areasCss}
	style:grid-template-rows={rowsCss}
>
	{#each areaNames as area (area)}
		{#if area === 'deckarea' || area === 'replica'}
			<div class="skel-region skel-deckarea" style:grid-area={area} data-region={area}>
				{#if area === 'replica'}
					<div class="skel-replica-grid">
						<div class="skel-box" data-region="deck-1">deck 1</div>
						<div class="skel-box skel-replica-mid" data-region="mixer">replica desk</div>
						<div class="skel-box" data-region="deck-2">deck 2</div>
						<div class="skel-box" data-region="deck-3">deck 3</div>
						<div class="skel-box" data-region="deck-4">deck 4</div>
					</div>
				{:else}
					<div class="skel-deck-grid">
						<div class="skel-box" data-region="deck-1">deck 1</div>
						<div class="skel-box skel-mixer" data-region="mixer">mixer</div>
						<div class="skel-box" data-region="deck-2">deck 2</div>
						<div class="skel-box" data-region="deck-3">deck 3</div>
						<div class="skel-box" data-region="deck-4">deck 4</div>
					</div>
				{/if}
			</div>
		{:else if area === 'wavestack'}
			<div class="skel-region" style:grid-area={area} data-region={area}>
				<div class="skel-box">wave rows</div>
			</div>
		{:else}
			<div class="skel-region" style:grid-area={area} data-region={area}>
				<div class="skel-box">{area}</div>
			</div>
		{/if}
	{/each}
</div>

<style>
	.perf-layout-skeleton {
		position: relative;
		display: grid;
		overflow: hidden;
		background: #0a0c10;
		color: #c8d0dc;
		font:
			12px/1.2 ui-sans-serif,
			system-ui,
			sans-serif;
		box-sizing: border-box;
	}
	.skel-region {
		min-height: 0;
		min-width: 0;
		border: 1px solid #3d4652;
		box-sizing: border-box;
	}
	.skel-box {
		display: grid;
		place-items: center;
		height: 100%;
		width: 100%;
		background: #141820;
		box-sizing: border-box;
	}
	.skel-deckarea {
		display: grid;
	}
	.skel-deck-grid {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 248px minmax(0, 1fr);
		grid-template-rows: 1fr 1fr;
		gap: 1px;
		height: 100%;
		min-height: 0;
		background: #0a0c10;
	}
	.skel-mixer {
		/* Middle column: the mixer spans both stacked deck rows, like Mixer.svelte
		   (grid-area: mixer) on /performance. Decks 1-4 auto-place around it. */
		grid-column: 2;
		grid-row: 1 / span 2;
	}
	.skel-replica-grid {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr) minmax(0, 1fr);
		grid-template-rows: 1fr auto;
		gap: 1px;
		height: 100%;
		min-height: 0;
		background: #0a0c10;
	}
	.skel-replica-mid {
		grid-column: 2;
		grid-row: 1 / span 2;
	}
	.skel-replica-grid > :nth-child(4) {
		grid-column: 1;
	}
	.skel-replica-grid > :nth-child(5) {
		grid-column: 3;
	}
</style>
