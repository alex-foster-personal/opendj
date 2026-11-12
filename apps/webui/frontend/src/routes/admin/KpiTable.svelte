<script lang="ts">
	/**
	 * BACKSTAGE-07: one KPI ledger as a table (shell.backstage on only; the
	 * flag-off page keeps KpiTile grids). Columns KPI / Latest / Move / Trend /
	 * Age; moved rows first by largest move; a filter box; never-measured KPIs
	 * fold into one line; readings older than STALE_KPI_DAYS get the warn shape.
	 * With `banner` the ledger's last run date and age head the table (farm).
	 *
	 * Read-only: nothing here talks to the audio engine, so it can never
	 * interrupt a playing deck. W/A/S/D move between rows like the arrow keys;
	 * a focused text field (the filter box) keeps its letters.
	 */
	import Sparkline from './Sparkline.svelte';
	import { buildKpiTable, ledgerStaleness } from './kpi-table';
	import { STALE_KPI_DAYS } from './status-rows';
	import './admin-state.css';
	import { utcToday } from './spark-geometry';
	import { tip } from './tooltip.svelte';
	import type { KpiDef, KpiSnapshot } from './kpi-api';
	import { moveRowFocus } from '$lib/shell/backstage-nav';

	interface Props {
		name: string;
		kpis: Record<string, KpiDef>;
		snapshots: KpiSnapshot[];
		timeAxis?: boolean;
		banner?: boolean;
		/** One-line source note for the ? hover. */
		source: string;
	}

	const { name, kpis, snapshots, timeAxis = false, banner = false, source }: Props = $props();

	const today = utcToday();
	let query = $state('');
	const total = $derived(Object.keys(kpis).length);
	const table = $derived(buildKpiTable(kpis, snapshots, today, query));
	const staleness = $derived(ledgerStaleness(snapshots, today));
	const neverAll = $derived(buildKpiTable(kpis, snapshots, today).never.length);

	let body: HTMLElement | undefined = $state();
	function onKeydown(event: KeyboardEvent): void {
		if (body !== undefined) moveRowFocus(event, body, 'tr[data-kpi-row]');
	}
</script>

<section class="kpit admin-st">
	<h3>
		{name}
		<span class="q" role="note" aria-label={source} title={source}>?</span>
	</h3>

	{#if banner && staleness.stale}
		<div class="alert warn" role="status" data-kpi-stale>
			<i class="st warn" aria-label="needs a look"></i>
			<span>
				Last run <b title={`Run ${staleness.lastLabel}`}>{staleness.lastDay}</b>,
				<b
					class="num"
					title={`Days between the last run and today (${today}). Warn past ${STALE_KPI_DAYS} days.`}
					>{staleness.ageDays}</b
				>
				days ago.
				<span class="num" title="KPIs in this ledger with no reading in any run">{neverAll}</span>
				of
				<span class="num" title="KPIs this ledger declares">{total}</span> never measured.
			</span>
		</div>
	{/if}

	<div class="toolrow">
		<input
			class="field"
			type="search"
			placeholder={`Filter ${total} KPIs`}
			aria-label="Filter KPIs"
			bind:value={query}
		/>
		<span
			class="hint"
			title="Moved rows first, largest move first. W/A/S/D or the arrow keys move between rows."
			>?</span
		>
	</div>

	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<table class="mt" onkeydown={onKeydown}>
		<thead>
			<tr>
				<th>KPI</th>
				<th class="r">Latest</th>
				<th>Move</th>
				<th>Trend</th>
				<th>Age</th>
			</tr>
		</thead>
		<tbody bind:this={body}>
			{#each table.rows as row (row.metric)}
				<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
				<tr data-kpi-row={row.metric} tabindex="0">
					<td>
						{row.kpi.label}
						{#if row.card.badge}<span class="origin" title="Origin of the latest reading"
								>{row.card.badge}</span
							>{/if}
						<span class="q" use:tip={row.card.tip} aria-label={row.kpi.title}>?</span>
					</td>
					<td class="r num" title={row.latestTitle}>{row.latestText}</td>
					<td class="num mv {row.move.verdict}" title={row.move.title}>{row.move.text}</td>
					<td class="trend"
						><Sparkline
							metric={row.metric}
							kpi={row.kpi}
							{snapshots}
							verdict={row.card.verdict}
							{timeAxis}
							{today}
						/></td
					>
					<td
						class="num age"
						title={`Measured ${row.measuredDay}; today is ${today}. Warn past ${STALE_KPI_DAYS} days.`}
					>
						{#if row.stale}<i class="st warn" aria-label="older than {STALE_KPI_DAYS} days"
							></i>{/if}
						{row.ageDays} d
					</td>
				</tr>
			{/each}
			{#if table.never.length > 0}
				<tr class="never" data-kpi-never>
					<td colspan="5">
						<i class="st idle" aria-hidden="true"></i>
						<span class="num" title={table.never.map((r) => r.kpi.label).join(', ')}
							>{table.never.length}</span
						>
						never measured
						<span class="q" title={table.never.map((r) => r.kpi.label).join(', ')}>?</span>
					</td>
				</tr>
			{/if}
			{#if table.rows.length === 0 && table.never.length === 0}
				<tr><td colspan="5" class="empty">No KPI matches "{query}".</td></tr>
			{/if}
		</tbody>
	</table>
</section>

<style>
	.kpit {
		max-width: 980px;
	}
	h3 {
		font-size: 1rem;
		color: var(--accent);
		margin: 0 0 0.5rem 0;
	}
	.q {
		display: inline-block;
		width: 1.1em;
		height: 1.1em;
		line-height: 1.1em;
		text-align: center;
		border: 1px solid var(--border);
		border-radius: 50%;
		font-size: 0.7rem;
		color: var(--muted);
		cursor: help;
		vertical-align: middle;
	}
	.alert {
		display: flex;
		gap: 10px;
		align-items: center;
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-left: 3px solid var(--st-warn);
		border-radius: 5px;
		font-size: 0.88rem;
		margin-bottom: 0.75rem;
	}
	.toolrow {
		display: flex;
		gap: 8px;
		align-items: center;
		margin-bottom: 0.5rem;
	}
	.field {
		background: var(--surface);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 5px;
		padding: 4px 8px;
		font: inherit;
		font-size: 0.85rem;
		min-width: 14rem;
	}
	.hint {
		color: var(--muted);
		cursor: help;
	}
	.mt {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}
	.mt th {
		text-align: left;
		color: var(--muted);
		font-weight: 500;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 4px 8px;
		border-bottom: 1px solid var(--border);
	}
	.mt td {
		padding: 4px 8px;
		border-bottom: 1px solid var(--border);
		vertical-align: middle;
		color: var(--fg);
	}
	.mt tr[data-kpi-row]:focus-visible,
	.mt tr[data-kpi-row]:hover {
		background: var(--chip-bg);
		outline: none;
	}
	.mt tr[data-kpi-row]:focus-visible {
		box-shadow: inset 2px 0 0 var(--accent);
	}
	.r {
		text-align: right !important;
	}
	.num {
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.mv.good {
		color: var(--st-ok);
	}
	.mv.bad {
		color: var(--st-err);
	}
	.mv.flat,
	.mv.none,
	.age,
	.never td,
	.empty {
		color: var(--muted) !important;
	}
	.trend {
		width: 96px;
	}
	.trend :global(.spark) {
		width: 90px;
		height: 22px;
	}
	.origin {
		font-size: 0.62rem;
		text-transform: uppercase;
		border: 1px solid var(--border);
		border-radius: 3px;
		padding: 0 0.25rem;
		color: var(--muted);
	}
	.st {
		display: inline-block;
		width: 9px;
		height: 9px;
		flex: 0 0 9px;
		vertical-align: middle;
	}
	.st.warn {
		border-radius: 50%;
		border: 2px solid var(--st-warn);
		box-sizing: border-box;
	}
	.st.idle {
		border-radius: 2px;
		border: 1.5px dashed var(--muted);
		box-sizing: border-box;
	}
</style>
