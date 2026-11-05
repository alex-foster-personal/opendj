<script lang="ts">
	/**
	 * Library footer readout for a multi-row selection (LIBUX played length):
	 * "8 selected · ~42m played · 61m total". Hidden under 2 selected rows.
	 * Agent twin: POST /api/v1/tracks/played-length, opendj track played-length,
	 * MCP tracks_played_length.
	 */
	import { estimatePlayedLength, formatPlayedLength, OVERLAP_BARS } from '$lib/rb/played-length';

	type Row = { order: number; duration_ms: number | null; bpm: number | null };

	let { rows, selectedOrders }: { rows: readonly Row[]; selectedOrders: readonly number[] } = $props();

	const estimate = $derived.by(() => {
		if (selectedOrders.length < 2) return null;
		const picked = new Set(selectedOrders);
		const inputs = [];
		for (const row of rows) {
			if (!picked.has(row.order)) continue;
			inputs.push({
				duration_s: row.duration_ms === null ? null : row.duration_ms / 1000,
				bpm: row.bpm,
				mix_in_s: null,
				mix_out_s: null
			});
		}
		return { count: inputs.length, ...estimatePlayedLength(inputs) };
	});
</script>

{#if estimate !== null && estimate.count >= 2}
	<div
		class="selection-played-length"
		role="status"
		data-testid="selection-played-length"
		title={`Rough mixed length: ${estimate.from_mix_points} with mix points, ${estimate.from_rule} by rule of thumb (minus a ${OVERLAP_BARS}-bar overlap at the track BPM, or 75% without one)${estimate.unknown ? `, ${estimate.unknown} without a duration` : ''}.`}
	>
		{estimate.count} selected · ~{formatPlayedLength(estimate.played_s)} played · {formatPlayedLength(estimate.total_s)} total
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
