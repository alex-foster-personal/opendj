<script lang="ts">
	/**
	 * Lyrics KPI ledger panel: the karaoke alignment / witness / no-lyrics
	 * numbers across measurement rounds (specs/karaoke-lyrics-alignment.md),
	 * rendered with the same tiles + run notes as the demucs farm panel.
	 *
	 * Data comes from GET /api/v1/bench/lyrics-kpi (the daemon serving
	 * scripts/bench/lyrics_kpi_ledger.json), never from the file directly, so
	 * an agent can curl exactly what is rendered here.
	 */
	import { onMount } from 'svelte';
	import KpiTile from './KpiTile.svelte';
	import RunNotes from './RunNotes.svelte';
	import type { KpiLedger } from './kpi-api';
	import { fetchLyricsKpiLedger } from './lyrics-api';

	let ledger = $state<KpiLedger | null>(null);
	let error = $state<string | null>(null);

	const metrics = $derived(ledger ? Object.entries(ledger.kpis) : []);
	const latestRun = $derived(ledger ? ledger.snapshots[ledger.snapshots.length - 1] : null);

	onMount(async () => {
		try {
			ledger = await fetchLyricsKpiLedger();
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		}
	});
</script>

<section class="panel">
	<h3>Lyrics KPI ledger</h3>
	<p class="sub">
		One card per lyrics-pipeline KPI, tracked across measurement rounds: alignment accuracy vs
		JamendoLyrics ground truth, ASR-witness quality, the no-lyrics detector, and library rollout
		counts. Hover a card for what the number means and where the reading came from.
	</p>

	{#if error}
		<div class="fatal">
			LOAD FAILED

			{error}

			The daemon serves this ledger from scripts/bench/lyrics_kpi_ledger.json. Check that the API
			is up and that the file parses.
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
			<code>uv run scripts/bench/lyrics_kpi_append.py --label &lt;round&gt; --set key=value --note
				"..."</code>.
		</p>
	{/if}
</section>

<style>
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
		margin: 0 0 0.75rem 0;
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
