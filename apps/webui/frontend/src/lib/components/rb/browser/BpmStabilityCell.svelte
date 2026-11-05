<script lang="ts">
	// Issue #7879: the BPM Stability cell's content. Renders the daemon's text and
	// tone verbatim; computes nothing. `· grid?` only for a suspect grid. The <td>
	// (and its title, `bpmStabilityTitle`) stays in TrackTable so its hide rule applies.
	import type { GridQualityRow } from '$lib/rb/analysis-issues';

	let { grid }: { grid: GridQualityRow | null | undefined } = $props();

	const tempo = $derived(grid?.tempo ?? null);
</script>

{#if tempo !== null}
	<span class="tempostab tone-{tempo.tone}"><span class="tempostab-arrow" aria-hidden="true">←</span> {tempo.text}</span
	>{#if grid?.grid_class === 'suspect'}<span class="tempostab-grid" title={grid.message ?? undefined}>{' · grid?'}</span>{/if}
{/if}

<style>
	/* Italic, about 1px smaller, dim thin arrow; tone by size of change. */
	.tempostab {
		font-style: italic;
		font-size: calc(var(--rb-fs-browser, 11px) - 1px);
	}
	.tempostab-arrow {
		opacity: 0.45;
		font-weight: 200;
		font-style: normal;
	}
	.tempostab.tone-none {
		color: var(--rb-text-dim, #8a8a8a);
	}
	.tempostab.tone-amber {
		color: #e0a23e;
	}
	.tempostab.tone-red {
		color: #e0443e;
	}
	.tempostab.tone-deep_red {
		color: #a8161d;
		font-weight: 600;
	}
	.tempostab-grid {
		font-style: italic;
		font-size: calc(var(--rb-fs-browser, 11px) - 1px);
		color: var(--rb-text-dim, #8a8a8a);
	}
</style>
