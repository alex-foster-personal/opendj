<script lang="ts">
	// Issue #10043: the Track Filter bar, one chip per active column funnel, an on/off switch that
	// suspends them without forgetting them, and Clear all (rekordbox Track Filter bar, mimicked).
	import {
		trackFilterChips,
		type TrackFilterColumn,
		type TrackFilterState
	} from './track-filter-state';

	let {
		filterState,
		shown,
		total,
		onsuspend,
		onclear,
		onopen
	}: {
		filterState: TrackFilterState;
		shown: number;
		total: number;
		onsuspend: (suspended: boolean) => void;
		onclear: () => void;
		onopen: (column: TrackFilterColumn) => void;
	} = $props();

	const chips = $derived(trackFilterChips(filterState));
</script>

<div
	class="track-filter-bar"
	class:suspended={filterState.suspended}
	role="toolbar"
	aria-label="Track filter"
>
	<button
		type="button"
		class="switch"
		aria-pressed={!filterState.suspended}
		title={filterState.suspended
			? 'Filters off: click to apply them again'
			: 'Filters on: click to show every row'}
		onclick={() => onsuspend(!filterState.suspended)}
	>
		Filter {filterState.suspended ? 'off' : 'on'}
	</button>
	{#each chips as chip (chip.column)}
		<button
			type="button"
			class="chip"
			data-column={chip.column}
			title="Edit this filter"
			onclick={() => onopen(chip.column)}>{chip.label}</button
		>
	{/each}
	<span class="count" aria-live="polite">{shown} of {total}</span>
	<button type="button" class="clear" onclick={onclear}>Clear all</button>
</div>

<style>
	.track-filter-bar {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 4px;
		padding: 2px 6px;
		border-bottom: 1px solid var(--rb-border, #444);
		font-size: 10px;
	}

	.track-filter-bar button {
		padding: 1px 6px;
		border: 1px solid var(--rb-border, #444);
		border-radius: 3px;
		background: transparent;
		color: inherit;
		font: inherit;
		cursor: pointer;
	}

	.switch[aria-pressed='true'],
	.chip {
		border-color: var(--rb-accent, #4fb2ff);
	}

	.suspended .chip {
		opacity: 0.5;
	}

	.count {
		margin-left: auto;
		color: var(--rb-text-dim, #888);
	}
</style>
