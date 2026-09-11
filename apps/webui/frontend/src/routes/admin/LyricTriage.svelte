<script lang="ts">
	/**
	 * Triage table over GET /api/v1/lyrics?order=suspect: the least
	 * trustworthy tracks first (operational-plan option C's seed). Row click
	 * opens the track page; the per-row buttons set or clear the human
	 * override via PUT /tracks/{id}/lyrics/override, which beats the computed
	 * verdict everywhere it is read.
	 */
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { putLyricOverride, type LyricVerdict, type LyricVerdictValue } from '$lib/api';
	import { fetchLyricTriage } from './lyrics-api';

	const LIMIT = 100;
	const RED_TITLE =
		'Share of words the independent ASR witness distrusts (contradict/lost classes). ' +
		'Calibration round 5: those classes carry ~4.5x the base alignment error rate, so a ' +
		'high red share means the timings deserve a listen, not that the text is wrong.';
	const OVERRIDE_CHOICES: { value: LyricVerdictValue; label: string; title: string }[] = [
		{ value: 'vocal', label: 'vocal', title: 'override: this track has real sung lyrics' },
		{
			value: 'sparse',
			label: 'sparse',
			title: 'override: only sparse vocals (chops, one-liners); lyric surfaces stay quiet'
		},
		{
			value: 'no-lyrics',
			label: 'no lyrics',
			title: 'override: instrumental; hide lyric surfaces for this track'
		}
	];

	let rows = $state<LyricVerdict[]>([]);
	let loaded = $state(false);
	let error = $state<string | null>(null);
	/** stable_id of the row whose override PUT is in flight; null when idle. */
	let busy = $state<string | null>(null);
	let overrideError = $state<string | null>(null);

	onMount(async () => {
		try {
			rows = await fetchLyricTriage({ order: 'suspect', limit: LIMIT });
			loaded = true;
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		}
	});

	function _open(stable_id: string): void {
		void goto(`/track/${stable_id}`);
	}

	function _onRowKeydown(event: KeyboardEvent, stable_id: string): void {
		if (event.key === 'Enter') _open(stable_id);
	}

	async function _setOverride(
		event: MouseEvent,
		row: LyricVerdict,
		value: LyricVerdictValue | null
	): Promise<void> {
		event.stopPropagation();
		busy = row.stable_id;
		overrideError = null;
		try {
			const updated = await putLyricOverride(row.stable_id, value);
			rows = rows.map((r) => (r.stable_id === row.stable_id ? updated : r));
		} catch (exc) {
			overrideError = exc instanceof Error ? exc.message : String(exc);
		} finally {
			busy = null;
		}
	}

	function _fmtPct(value: number | null): string {
		return value === null ? '-' : `${Math.round(value * 10) / 10}%`;
	}
</script>

<div class="triage">
	{#if error}
		<div class="error">
			LOAD FAILED

			{error}
		</div>
	{:else if !loaded}
		<p class="copy">Loading triage listing...</p>
	{:else if rows.length === 0}
		<p class="copy">
			No lyric verdicts in state.db yet. Run
			<code>python -m apps.lyrics ingest-state</code> on a finished bench run to populate them.
		</p>
	{:else}
		{#if overrideError}
			<div class="error">
				OVERRIDE FAILED

				{overrideError}
			</div>
		{/if}
		<table>
			<thead>
				<tr>
					<th>track</th>
					<th>verdict</th>
					<th class="num">coverage</th>
					<th>source</th>
					<th class="num">red</th>
					<th class="num">words</th>
					<th>override</th>
				</tr>
			</thead>
			<tbody>
				{#each rows as row (row.stable_id)}
					<tr
						class="row"
						tabindex="0"
						title="open /track/{row.stable_id}"
						onclick={() => _open(row.stable_id)}
						onkeydown={(e) => _onRowKeydown(e, row.stable_id)}
					>
						<td class="id"><code>{row.stable_id.slice(0, 10)}</code></td>
						<td>
							<span
								class="chip v-{row.effective}"
								title={row.override
									? `human override (${row.override}); computed verdict was ${row.verdict}` +
										(row.override_note ? ` - note: ${row.override_note}` : '')
									: 'computed from stem vocal coverage'}
							>
								{row.effective}
								{#if row.override}<b aria-hidden="true">*</b>{/if}
							</span>
						</td>
						<td
							class="num"
							title="Stem vocal coverage: share of the track where the separated vocal stem is audibly active."
						>
							{_fmtPct(row.coverage_pct ?? null)}
						</td>
						<td class="src" title="Lyric provider + match method that supplied this text.">
							{row.source ?? '-'}
						</td>
						<td class="num" title={RED_TITLE}>{_fmtPct(row.pct_witness_red ?? null)}</td>
						<td class="num" title="Number of aligned words stored for this track.">
							{row.n_words ?? '-'}
						</td>
						<td class="overrides">
							{#each OVERRIDE_CHOICES as choice (choice.value)}
								<button
									type="button"
									class:active={row.override === choice.value}
									disabled={busy === row.stable_id}
									title={choice.title}
									onclick={(e) => _setOverride(e, row, choice.value)}
								>
									{choice.label}
								</button>
							{/each}
							<button
								type="button"
								class="clear"
								disabled={busy === row.stable_id || row.override === null}
								title={row.override === null
									? 'no override set - the computed verdict is already in effect'
									: 'clear the human override and fall back to the computed verdict'}
								onclick={(e) => _setOverride(e, row, null)}
							>
								clear
							</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
		<p class="copy foot">
			{rows.length} row(s), most suspect first (witness red share, then coverage). Showing at most
			{LIMIT}; the full listing is
			<code>GET /api/v1/lyrics?order=suspect</code>.
		</p>
	{/if}
</div>

<style>
	.copy {
		color: var(--muted);
		font-size: 0.8rem;
		margin: 0 0 0.6rem 0;
		max-width: 80ch;
	}
	.foot {
		margin-top: 0.5rem;
	}
	table {
		border-collapse: collapse;
		width: 100%;
		font-size: 0.8rem;
	}
	th {
		text-align: left;
		color: var(--muted);
		font-weight: 500;
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.25rem 0.6rem;
		border-bottom: 1px solid var(--border);
	}
	td {
		padding: 0.3rem 0.6rem;
		border-bottom: 1px solid var(--border);
		vertical-align: middle;
	}
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
		cursor: help;
	}
	tr.row {
		cursor: pointer;
	}
	tr.row:hover,
	tr.row:focus-visible {
		background: var(--surface-hover);
		outline: none;
	}
	.id code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		font-size: 0.75rem;
	}
	.src {
		color: var(--muted);
		cursor: help;
		max-width: 14rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.chip {
		border: 1px solid var(--border);
		border-radius: 999px;
		padding: 0.1rem 0.55rem;
		font-size: 0.72rem;
		cursor: help;
		white-space: nowrap;
	}
	.chip.v-vocal {
		color: var(--kpi-ok);
		border-color: var(--kpi-ok);
	}
	.chip.v-sparse {
		color: var(--accent);
		border-color: var(--accent);
	}
	.chip.v-no-lyrics,
	.chip.v-unknown {
		color: var(--muted);
	}
	.chip b {
		margin-left: 0.15rem;
	}
	.overrides {
		white-space: nowrap;
	}
	.overrides button {
		background: var(--chip-bg);
		color: var(--fg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 0.1rem 0.45rem;
		font-size: 0.7rem;
		cursor: pointer;
		margin-right: 0.2rem;
	}
	.overrides button.active {
		border-color: var(--accent);
		color: var(--accent);
	}
	.overrides button:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.error {
		background: var(--danger);
		color: #fff;
		padding: 0.6rem 0.8rem;
		border-radius: 6px;
		font-size: 0.8rem;
		font-weight: 600;
		white-space: pre-wrap;
		line-height: 1.45;
		margin-bottom: 0.5rem;
		max-width: 40rem;
	}
	code {
		background: var(--chip-bg);
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
	}
</style>
