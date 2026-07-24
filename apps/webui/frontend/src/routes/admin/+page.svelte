<script lang="ts">
	/**
	 * /admin - operator panel. First (and currently only) section is the demucs
	 * farm KPI ledger, the durable home for what used to be the standalone
	 * scripts/bench/kpi_chart.html.
	 *
	 * Data comes from GET /api/v1/bench/kpi, never from the file directly, so an
	 * agent can curl exactly what is rendered here. Errors are loud: a daemon
	 * that cannot serve the ledger renders a banner, not an empty grid.
	 */
	import { onMount } from 'svelte';
	import KpiTile from './KpiTile.svelte';
	import RunNotes from './RunNotes.svelte';
	import TipLayer from './TipLayer.svelte';
	import { fetchKpiLedger, type KpiLedger } from './kpi-api';

	let ledger = $state<KpiLedger | null>(null);
	let error = $state<string | null>(null);

	const metrics = $derived(ledger ? Object.entries(ledger.kpis) : []);
	const latestRun = $derived(ledger ? ledger.snapshots[ledger.snapshots.length - 1] : null);

	onMount(async () => {
		try {
			ledger = await fetchKpiLedger();
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		}
	});
</script>

<h2>Admin</h2>

<section class="panel">
	<h3>Demucs farm KPI ledger</h3>
	<p class="sub">
		One card per KPI, tracked across farm runs. The latest reading is the big number and the arrow
		is the move since the previous reading, green when it moved the good way for that KPI, red when
		it moved the wrong way. Hover anywhere on a card for what the number means and why it matters.
	</p>
	<p class="note">
		A dash or a hollow dashed mark in a sparkline means that KPI was not measured in that run, which
		is different from a real zero. <code>fixed_overhead_s</code> is genuinely 0 on Modal (no VM to
		boot); that is not the same thing as unmeasured. Durations render as
		<code>23m 24s</code> rather than raw seconds; sub-minute readings stay in seconds.
	</p>
	<p class="note">
		Cards with no badge are <strong>derived</strong>: recomputed from per-track cache telemetry by
		<code>scripts/bench/kpi_derive.py</code>, so nobody typed them. A <code>typed</code> badge means
		hand-entered with no telemetry behind it, and <code>config</code> means the value was copied
		from a config constant and is a ceiling, not a measurement. Hover for the exact origin.
	</p>

	{#if error}
		<div class="fatal">
			LOAD FAILED

			{error}

			The daemon serves this ledger from scripts/bench/kpi_ledger.json. Check that the API is up on
			:8585 and that the file parses.
		</div>
	{:else if !ledger}
		<p class="sub">Loading ledger...</p>
	{:else}
		<div class="grid">
			{#each metrics as [metric, kpi] (metric)}
				<KpiTile {metric} {kpi} snapshots={ledger.snapshots} />
			{/each}
		</div>

		<RunNotes kpis={ledger.kpis} snapshots={ledger.snapshots} />

		<p class="footer">
			{ledger.snapshots.length} snapshot(s); latest <code>{latestRun?.label}</code> at
			<code>{latestRun?.ts}</code>. Append with
			<code>uv run scripts/bench/kpi_append.py --label &lt;run&gt; --set key=value --note "..."</code>.
		</p>
	{/if}
</section>

<TipLayer />

<style>
	/* No success/positive token exists in app.css yet; scoped here rather than
	 * editing that shared file mid-fan-out. */
	:global(:root) {
		--kpi-ok: #4ecb8c;
	}
	:global(html[data-theme='light']) {
		--kpi-ok: #2e9e63;
	}

	.panel {
		max-width: 1180px;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.25rem 0;
	}
	.sub {
		color: var(--muted);
		font-size: 0.85rem;
		margin: 0 0 0.5rem 0;
		max-width: 90ch;
	}
	.note {
		color: var(--muted);
		font-size: 0.78rem;
		margin: 0 0 1.1rem 0;
		max-width: 90ch;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
		gap: 0.75rem;
	}
	.footer {
		color: var(--muted);
		font-size: 0.75rem;
		margin-top: 1.25rem;
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
