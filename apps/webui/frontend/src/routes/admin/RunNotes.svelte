<script lang="ts">
	/**
	 * Author notes, one page per run.
	 *
	 * The cards say what moved; this says why it moved and how much of it to
	 * believe. Text comes from the snapshot's `notes` field in
	 * scripts/bench/kpi_ledger.json (written by `kpi_append.py --note`), so a
	 * run with no note says so rather than showing invented commentary.
	 */
	import { untrack } from 'svelte';
	import { formatWithUnit } from './format';
	import type { KpiDef, KpiSnapshot } from './kpi-api';

	interface Props {
		kpis: Record<string, KpiDef>;
		snapshots: KpiSnapshot[];
	}

	const { kpis, snapshots }: Props = $props();

	// Opens on the newest run; untracked so a later ledger refetch does not yank
	// the reader off the page they chose.
	let index = $state(untrack(() => snapshots.length - 1));

	const current = $derived(snapshots[index]);
	const paragraphs = $derived(current.notes ? current.notes.split('\n\n') : []);
	const measured = $derived(
		Object.entries(current.values)
			.filter((entry): entry is [string, number] => typeof entry[1] === 'number')
			.filter(([metric]) => metric in kpis)
			.map(([metric, value]) => ({
				metric,
				label: kpis[metric].label,
				text: formatWithUnit(value, kpis[metric].unit)
			}))
	);

	function step(by: number): void {
		index = Math.min(snapshots.length - 1, Math.max(0, index + by));
	}
</script>

<section class="notes">
	<header>
		<h3>Run notes</h3>
		<div class="pager">
			<button type="button" onclick={() => step(-1)} disabled={index === 0} title="Previous run">
				Prev
			</button>
			<select
				bind:value={index}
				title="Jump to a run. Notes are paginated one page per snapshot."
			>
				{#each snapshots as snapshot, i}
					<option value={i}>{i + 1}. {snapshot.label}</option>
				{/each}
			</select>
			<button
				type="button"
				onclick={() => step(1)}
				disabled={index === snapshots.length - 1}
				title="Next run"
			>
				Next
			</button>
			<span class="count" title="Which run page you are on, out of the runs in the ledger.">
				run {index + 1} of {snapshots.length}
			</span>
		</div>
	</header>

	<div class="run-head">
		<span class="run-label">{current.label}</span>
		<span class="run-ts" title="When this snapshot was appended to the ledger (UTC).">
			{current.ts}
		</span>
	</div>

	{#if measured.length > 0}
		<div class="measured">
			{#each measured as item}
				<span class="chip" title="{kpis[item.metric].title} Recorded in this run.">
					{item.label}: <strong>{item.text}</strong>
				</span>
			{/each}
		</div>
	{:else}
		<p class="empty">No KPI values were recorded in this snapshot.</p>
	{/if}

	{#if paragraphs.length > 0}
		{#each paragraphs as para}
			<p>{para}</p>
		{/each}
	{:else}
		<p class="empty">
			No note recorded for this run. Add one with
			<code>uv run scripts/bench/kpi_append.py --label {current.label} --note "..."</code>
			on the next append.
		</p>
	{/if}
</section>

<style>
	.notes {
		margin-top: 1.5rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.85rem 1rem 1rem;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		flex-wrap: wrap;
	}
	h3 {
		margin: 0;
		font-size: 0.95rem;
		color: var(--accent);
	}
	.pager {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
	.pager button {
		background: var(--chip-bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.2rem 0.6rem;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.pager button:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.pager select {
		background: var(--chip-bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.2rem 0.4rem;
		font-size: 0.78rem;
		max-width: 16rem;
	}
	.count {
		font-size: 0.72rem;
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}
	.run-head {
		display: flex;
		align-items: baseline;
		gap: 0.6rem;
		margin: 0.6rem 0 0.5rem;
		flex-wrap: wrap;
	}
	.run-label {
		font-size: 0.9rem;
		font-weight: 600;
	}
	.run-ts {
		font-size: 0.72rem;
		color: var(--muted);
		font-variant-numeric: tabular-nums;
	}
	.measured {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
		margin-bottom: 0.6rem;
	}
	.chip {
		background: var(--chip-bg);
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 0.15rem 0.6rem;
		font-size: 0.72rem;
		color: var(--muted);
		cursor: help;
	}
	.chip strong {
		color: var(--fg);
		font-variant-numeric: tabular-nums;
	}
	.notes p {
		margin: 0 0 0.7rem 0;
		font-size: 0.85rem;
		line-height: 1.55;
		max-width: 78ch;
	}
	.empty {
		color: var(--muted);
	}
	code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		font-size: 0.78rem;
	}
</style>
