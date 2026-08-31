<script lang="ts">
	/**
	 * Code-quality ratchet: the merge gate's current allowance table.
	 *
	 * Data comes from GET /api/v1/admin/quality-ratchet, which serves
	 * ops/quality/baseline.json verbatim. That file is kept current by
	 * scripts/quality_gate.py itself: `just quality` fails any commit that
	 * makes a tracked metric worse, and only `just quality-baseline` may move
	 * a number, and only downward. So "generated" below is not a stale
	 * snapshot date, it is the ratchet's own bookkeeping of when it last
	 * moved - self-dating is why this section can say "as of" honestly
	 * without a live gate run on every page load.
	 *
	 * One exception rendered specially: arch.contracts_broken is HARD-GATED
	 * at zero regardless of baseline (see ops/quality/README.md), so it never
	 * reads as "an allowance that could rise" like every other row here.
	 */
	import { onMount } from 'svelte';
	import { fetchQualityRatchet, type QualityRatchet } from './quality-api';
	import { QUALITY_METRIC_DEFS } from './quality-why';

	let ratchet = $state<QualityRatchet | null>(null);
	let error = $state<string | null>(null);

	const rows = $derived(
		ratchet
			? Object.entries(ratchet.metrics).map(([key, value]) => ({
					key,
					value,
					def: QUALITY_METRIC_DEFS[key] ?? { label: key, unit: '', title: 'No description recorded for this metric key.' }
				}))
			: []
	);

	function formatMetric(value: number, unit: string): string {
		return unit === '%' ? `${value}%` : String(value);
	}

	onMount(async () => {
		try {
			ratchet = await fetchQualityRatchet();
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		}
	});
</script>

<section class="panel">
	<h3>Code quality ratchet</h3>
	<p class="sub">
		The merge gate's current allowance table: every metric below is measured on every merge by
		<code>scripts/quality_gate.py</code>, and a commit that makes any of them worse fails the
		gate. This is a ratchet, not a standard - allowances only ever shrink, and only a deliberate
		<code>just quality-baseline</code> run moves one, and only downward. Hover a row for what it
		measures.
	</p>

	{#if error}
		<div class="fatal">
			LOAD FAILED

			{error}

			The daemon serves this table from ops/quality/baseline.json. Check that the API is up and
			that the file parses.
		</div>
	{:else if !ratchet}
		<p class="sub">Loading ratchet...</p>
	{:else}
		<div class="rows">
			{#each rows as row (row.key)}
				<div
					class="row"
					class:hard-gated={row.def.hardGated}
					title="{row.def.title} Key: {row.key}."
				>
					<span class="label">
						{row.def.label}
						{#if row.def.hardGated}<span class="hard-badge">hard-gated</span>{/if}
					</span>
					<span class="value">
						{formatMetric(row.value, row.def.unit)}
						{#if row.def.unit && row.def.unit !== '%'}<span class="unit">{row.def.unit}</span>{/if}
					</span>
				</div>
			{/each}
		</div>

		<p class="footer">
			Recorded <code>{ratchet.generated}</code> in <code>ops/quality/baseline.json</code>. Re-run
			with <code>just quality</code>; bank an improvement with
			<code>just quality-baseline</code>.
		</p>
	{/if}
</section>

<style>
	.panel {
		max-width: 1180px;
		margin-top: 1.5rem;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.25rem 0;
	}
	.sub {
		color: var(--muted);
		font-size: 0.85rem;
		margin: 0 0 0.75rem 0;
		max-width: 90ch;
	}
	.rows {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 0.5rem;
	}
	.row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.5rem 0.7rem;
		cursor: help;
	}
	.row:hover {
		border-color: var(--accent-dim);
		background: var(--surface-hover);
	}
	.row.hard-gated {
		border-color: var(--kpi-ok);
	}
	.label {
		font-size: 0.78rem;
		color: var(--muted);
		line-height: 1.25;
	}
	.hard-badge {
		display: inline-block;
		margin-left: 0.35rem;
		font-size: 0.6rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		border: 1px solid var(--kpi-ok);
		color: var(--kpi-ok);
		border-radius: 3px;
		padding: 0 0.25rem;
	}
	.value {
		font-size: 1rem;
		font-weight: 600;
		color: var(--fg);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.unit {
		font-size: 0.72rem;
		font-weight: 400;
		color: var(--muted);
		margin-left: 0.2rem;
	}
	.footer {
		color: var(--muted);
		font-size: 0.75rem;
		margin-top: 1rem;
	}
	.fatal {
		background: var(--danger);
		color: #fff;
		padding: 1rem 1.25rem;
		border-radius: 8px;
		font-weight: 600;
		white-space: pre-wrap;
		line-height: 1.5;
	}
	code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
	}
</style>
