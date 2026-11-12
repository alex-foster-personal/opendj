<script lang="ts">
	/**
	 * Backstage Status overview (BACKSTAGE-06). Reads only endpoints the app
	 * already calls; derivation lives in status-rows.ts. Read-only: nothing
	 * here talks to the audio engine, so it can never interrupt a playing deck.
	 * W/A/S/D move focus between rows like the arrow keys.
	 */
	import { onMount } from 'svelte';
	import { getHealth } from '$lib/api';
	import { rekordboxWriteback } from '$lib/rb/rekordbox-writeback.svelte';
	// Through the shared reader, not api-rb directly: api-rb's fan-in is ratcheted.
	import { readReconcileSummaryOnce } from '$lib/rb/reconcile-summary-refresh';
	import { moveRowFocus } from '$lib/shell/backstage-nav';
	import './admin-state.css';
	import { fetchDedupClusters } from '../dedup/dedup-api';
	import { fetchKpiLedger, fetchPerfKpiLedger, type KpiLedger } from './kpi-api';
	import {
		allClearLine,
		deriveStatusRows,
		INERT_AREAS,
		splitStatus,
		STATE_LABEL,
		type Probe,
		type StatusInputs
	} from './status-rows';

	const loading = { kind: 'loading' } as const;
	let health = $state<StatusInputs['health']>(loading);
	let writeback = $state<StatusInputs['writeback']>(loading);
	let dedup = $state<StatusInputs['dedup']>(loading);
	let missing = $state<StatusInputs['missing']>(loading);
	let perf = $state<StatusInputs['perf']>(loading);
	let farm = $state<StatusInputs['farm']>(loading);

	const view = $derived(
		splitStatus(
			deriveStatusRows({ health, writeback, dedup, missing, perf, farm, now: new Date() })
		)
	);
	const clearLine = $derived(allClearLine(view.clear));

	const msg = (exc: unknown) => (exc instanceof Error ? exc.message : String(exc));
	function settle<T>(p: Promise<T>, set: (v: Probe<T>) => void): void {
		p.then(
			(value) => set({ kind: 'ok', value }),
			(exc) => set({ kind: 'error', message: msg(exc) })
		);
	}
	const latest = (l: KpiLedger) => ({
		latestTs: l.snapshots.length ? l.snapshots[l.snapshots.length - 1].ts : null,
		kpis: Object.keys(l.kpis).length
	});

	onMount(() => {
		settle(
			getHealth().then(({ health: h }) => ({
				status: h.status,
				tracks: h.state_db.tracks,
				tracksPlayable: h.state_db.tracks_playable
			})),
			(v) => (health = v)
		);
		void rekordboxWriteback.probe().then(() => {
			writeback =
				rekordboxWriteback.error !== null
					? { kind: 'error', message: rekordboxWriteback.error }
					: {
							kind: 'ok',
							value: { enabled: rekordboxWriteback.enabled, title: rekordboxWriteback.title }
						};
		});
		settle(
			fetchDedupClusters().then((r) => ({
				undecided: r.clusters.filter((c) => c.decision === null).length,
				total: r.clusters.length,
				note: r.note
			})),
			(v) => (dedup = v)
		);
		let applied = false;
		void readReconcileSummaryOnce((read) => {
			applied = true;
			missing = read.counts
				? {
						kind: 'ok',
						value: {
							warming: false,
							broken: read.counts.broken,
							total: read.counts.nonBroken + read.counts.broken
						}
					}
				: { kind: 'error', message: read.error ?? 'reconcile summary unavailable' };
		}).then((outcome) => {
			// A first scan still running applies nothing and reports refreshing.
			if (!applied && outcome.ok) missing = { kind: 'ok', value: { warming: true } };
		});
		settle(fetchPerfKpiLedger().then(latest), (v) => (perf = v));
		settle(fetchKpiLedger().then(latest), (v) => (farm = v));
	});

	let list: HTMLElement | undefined = $state();
	function onKeydown(event: KeyboardEvent): void {
		if (list !== undefined) moveRowFocus(event, list, 'a[data-status-row]');
	}
</script>

<section class="panel status admin-st" data-testid="backstage-status">
	<h3>Status</h3>
	<p class="sub">
		Every area that needs a look, from the live endpoints. W/A/S/D or the arrow keys move between
		rows.
	</p>

	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="rows" bind:this={list} onkeydown={onKeydown}>
		{#each view.attention as row (row.id)}
			<a
				class="st-row {row.state}"
				href={row.href}
				title={row.title}
				data-status-row={row.id}
				data-state={row.state}
			>
				<i class="st {row.state}" aria-label={STATE_LABEL[row.state]}></i>
				<span class="area">{row.area}</span>
				<span class="line">
					{#each row.line as seg, i (i)}{#if typeof seg === 'string'}{seg}{:else}<span
								class="num"
								title={seg.title}>{seg.n}</span
							>{/if}{/each}
				</span>
			</a>
		{/each}
		{#if clearLine !== null}
			<a
				class="st-row ok clear"
				href={view.clear[0].href}
				title={view.clear
					.map((r) => `${r.area}: ${r.line.map((s) => (typeof s === 'string' ? s : s.n)).join('')}`)
					.join('\n')}
				data-status-row="clear"
				data-state="ok"
			>
				<i class="st ok" aria-label={STATE_LABEL.ok}></i>
				<span class="line">{clearLine}</span>
			</a>
		{/if}
	</div>

	<p class="sub-h">Not reported yet</p>
	<div class="rows">
		{#each INERT_AREAS as a (a.id)}
			<span class="st-row inert" aria-disabled="true" title={a.title} data-inert={a.id}>
				<i class="st idle" aria-label="not implemented"></i>
				<span class="area">{a.area}</span>
				<span class="line">{a.title}</span>
			</span>
		{/each}
	</div>
</section>

<style>
	.status {
		max-width: 900px;
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
	}
	.sub-h {
		color: var(--muted);
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin: 1.25rem 0 0.4rem;
	}
	.rows {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.st-row {
		display: flex;
		gap: 10px;
		align-items: center;
		padding: 8px 10px;
		border: 1px solid var(--border, #1c222c);
		border-left-width: 3px;
		border-radius: 5px;
		color: var(--fg);
		text-decoration: none;
		font-size: 0.88rem;
	}
	.st-row.warn {
		border-left-color: var(--st-warn);
	}
	.st-row.err {
		border-left-color: var(--st-err);
	}
	.st-row.ok {
		border-left-color: var(--st-ok);
	}
	a.st-row:hover,
	a.st-row:focus-visible {
		background: var(--chip-bg);
		outline: none;
	}
	a.st-row:focus-visible {
		box-shadow: 0 0 0 2px var(--accent);
	}
	.st-row.inert {
		color: var(--muted);
		opacity: 0.7;
		cursor: default;
	}
	.area {
		font-weight: 600;
		min-width: 9.5rem;
	}
	.line {
		color: var(--muted);
	}
	.st-row.warn .line,
	.st-row.err .line {
		color: var(--fg);
	}
	.num {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
	}
	.st {
		display: inline-block;
		width: 9px;
		height: 9px;
		flex: 0 0 9px;
	}
	.st.ok {
		border-radius: 50%;
		background: var(--st-ok);
	}
	.st.warn {
		border-radius: 50%;
		border: 2px solid var(--st-warn);
		box-sizing: border-box;
	}
	.st.err {
		width: 0;
		height: 0;
		border-left: 5px solid transparent;
		border-right: 5px solid transparent;
		border-bottom: 9px solid var(--st-err);
	}
	.st.idle {
		border-radius: 2px;
		border: 1.5px dashed var(--muted);
		box-sizing: border-box;
	}
</style>
