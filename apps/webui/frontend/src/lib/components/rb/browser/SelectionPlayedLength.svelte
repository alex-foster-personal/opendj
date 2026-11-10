<script lang="ts">
	/**
	 * Library list footer: visible row count and summed duration, plus the
	 * selection when 2 or more rows are selected (rekordbox parity).
	 * Agent twin: POST /api/v1/tracks/list-totals, opendj track list-totals,
	 * MCP tracks_list_totals.
	 */
	import {
		formatLibraryFooter,
		summarizeListTotals,
		type DurationRow
	} from '$lib/rb/list-totals';

	type Row = DurationRow & { order: number };

	let {
		visibleRows,
		rows,
		selectedOrders
	}: {
		visibleRows: readonly Row[];
		/** Full pane rows for resolving multi-select by order. */
		rows: readonly Row[];
		selectedOrders: readonly number[];
	} = $props();

	const listTotals = $derived(summarizeListTotals(visibleRows));

	const selectionTotals = $derived.by(() => {
		if (selectedOrders.length < 2) return null;
		const picked = new Set(selectedOrders);
		const selected: Row[] = [];
		for (const row of rows) {
			if (!picked.has(row.order)) continue;
			selected.push(row);
		}
		return summarizeListTotals(selected);
	});

	const line = $derived(formatLibraryFooter(listTotals, selectionTotals));

	const listTitle = $derived.by(() => {
		const { count, duration_ms, unknown } = listTotals;
		const parts = [`${count} visible rows after the active filters`, `summed duration ${duration_ms} ms`];
		if (unknown > 0) parts.push(`${unknown} without a known duration (excluded from the time)`);
		return parts.join('; ');
	});
</script>

{#if listTotals.count > 0 || (selectionTotals !== null && selectionTotals.count >= 2)}
	<div
		class="selection-played-length"
		role="status"
		data-testid="library-list-totals"
		title={listTitle}
	>
		{line}
	</div>
{/if}

<style>
	.selection-played-length {
		padding: 2px 8px;
		font-size: 11px;
		color: var(--rb-text-dim, #9a9a9a);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
</style>
