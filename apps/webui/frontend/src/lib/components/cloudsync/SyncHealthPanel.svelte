<script lang="ts">
	/**
	 * "Do my machines agree?" -- the one readout the CloudSync page exists to
	 * answer, and the reason the sync engine stopped being invisible.
	 *
	 * Every field comes from GET /api/v1/cloudsync/overview's `sync` block,
	 * which is apps.sync_hub.health.FleetHealth.to_wire() verbatim, so an
	 * agent and a human never read two different answers (agent-native
	 * parity).
	 *
	 * House rule: every numeric readout here carries a hover `title` saying
	 * what the number is. These are watermarks and changelog sequences --
	 * nobody should have to read the schema to interpret one.
	 *
	 * Its own component rather than inline on the page for the quality gate's
	 * file_size ratchet: the panel took +page.svelte over 600 lines, and the
	 * boundary is real (the page is a config surface; this is a readout).
	 */
	import type { SyncHealth, SyncVerdict } from '$lib/api-cloudsync';

	let { health }: { health: SyncHealth } = $props();
	const s = $derived(health);
	const run = $derived(health.last_run);

	/**
	 * The one question the sync readout answers, in the DJ's words.
	 *
	 * Exhaustive `Record`s over the verdict union rather than a lookup with a
	 * fallback: a verdict added on the server and not here becomes a
	 * TypeScript error at build time instead of a blank chip on the page.
	 */
	const VERDICT_LABEL: Record<SyncVerdict, string> = {
		diverged: 'Diverged',
		failing: 'Sync failing',
		never_synced: 'Never synced',
		unknown: 'Unknown',
		quarantined: 'Rows held back',
		behind: 'Behind',
		converged: 'In sync'
	};

	const VERDICT_BLURB: Record<SyncVerdict, string> = {
		diverged:
			'The last sync finished and the two sides still disagree. Nothing repairs this automatically; check the machines before trusting either library.',
		failing: 'The last sync attempt did not complete. Nothing has been lost; it has not caught up.',
		never_synced: 'This machine has not synced against a hub yet, so it owes the hub everything.',
		unknown:
			'This machine has sync watermarks but no recorded outcome, so nothing here can say whether the last attempt worked.',
		quarantined:
			'Some rows are not in the sync set and reach no other machine: a timestamp this machine cannot order, or a row that references one. Every other row syncs normally. Run `python -m apps.shared.state.normalize_stamps --live` to put them back on the UTC line.',
		behind:
			'Local edits have not reached the hub yet. Run a sync before walking out with this machine.',
		converged: 'Everything this machine has written has reached the hub.'
	};

	const VERDICT_TONE: Record<SyncVerdict, 'ok' | 'warn' | 'bad' | 'muted'> = {
		diverged: 'bad',
		failing: 'bad',
		never_synced: 'muted',
		unknown: 'muted',
		quarantined: 'warn',
		behind: 'warn',
		converged: 'ok'
	};
</script>

<div class="sync-health" class:bad={VERDICT_TONE[s.verdict] === 'bad'}>
	<div class="verdict-row">
		<span class="verdict {VERDICT_TONE[s.verdict]}" title={VERDICT_BLURB[s.verdict]}>
			{VERDICT_LABEL[s.verdict]}
		</span>
		<span style="color: var(--muted);">{VERDICT_BLURB[s.verdict]}</span>
	</div>

	<dl class="readouts">
		<div>
			<dt>Pending push</dt>
			<dd
				class:warn={(s.pending_push_rows ?? 0) > 0}
				title="Rows this machine has written that the hub furthest behind has not received yet. Counted as distinct (table, row) entries in local_changelog above that peer's push fence, so it is an upper bound on what the next push offers -- playlist membership folds into its parent playlist on the wire. Blank when no hub has been met yet, because the next push is then the whole library, which no changelog count describes."
			>
				{s.pending_push_rows === null ? 'n/a' : s.pending_push_rows}
			</dd>
		</div>
		<div>
			<dt>Held back</dt>
			<dd
				class:warn={s.quarantined_rows > 0}
				title="Rows on this machine that are NOT in the sync set. Either a stored updated_at or deleted_at on them cannot be placed on the UTC line (a legacy naive stamp, or SQLite's own CURRENT_TIMESTAMP spelling), or they reference a row that is held back for that reason -- a track held back takes its locations, vendor ids and fields with it, and a playlist whose membership names one is held back whole, because sending a child without its parent is a foreign-key error the peer cannot retry past. They reach no other machine and are excluded from the digest, so the digest still compares two settled states. Counted once per row, not per column, and it is the same count the digest excluded. Clear it with `python -m apps.shared.state.normalize_stamps --live`."
			>
				{s.quarantined_rows}
			</dd>
		</div>
		<div>
			<dt>Local writes</dt>
			<dd
				title="This machine's own write counter: the greatest local_changelog.seq, i.e. how many synced-table writes it has recorded since the changelog began. Only ever goes up."
			>
				{s.local_seq}
			</dd>
		</div>
		<div>
			<dt>Peers</dt>
			<dd
				title="Hubs this machine holds a sync watermark against (the machine-local sync_state table). One in the usual hub-and-spoke setup."
			>
				{s.peers.length}
			</dd>
		</div>
		<div>
			<dt>Failure streak</dt>
			<dd
				class:warn={(run?.consecutive_failures ?? 0) > 0}
				title="Sync attempts since the last successful one. 0 means the last attempt worked. A long streak means the hub has been unreachable, not that anything has been lost."
			>
				{run?.consecutive_failures ?? 0}
			</dd>
		</div>
		<div>
			<dt>Last attempt</dt>
			<dd
				title={run
					? `${run.outcome} at ${run.finished_at} against ${run.hub_url}`
					: 'no sync attempt has been recorded on this machine'}
			>
				{run ? run.finished_at : 'never'}
			</dd>
		</div>
	</dl>

	{#if run && run.error_message}
		<p class="sync-error" title={`${run.error_kind} raised by the last sync attempt`}>
			{run.error_kind}: {run.error_message}
		</p>
	{:else if run}
		<p style="color: var(--muted); font-size: 0.82rem;">
			Last sync pushed
			<span title="Rows this machine offered the hub on the last successful sync">{run.pushed}</span
			>
			(<span
				title="Rows the hub accepted; the rest lost a last-writer-wins comparison and were rejected on purpose"
				>{run.accepted}</span
			>
			accepted,
			<span title="Rows the hub rejected because it already held a newer version. Not an error."
				>{run.rejected}</span
			>
			rejected), pulled
			<span title="Rows the hub sent back on the last successful sync">{run.pulled}</span>
			(<span title="Rows from the hub this machine applied locally">{run.applied}</span>
			applied) over
			<span
				title="Push/pull passes the sync needed before both sides stopped moving. More than 1 means another machine was writing during the round trip."
				>{run.rounds}</span
			>
			round(s).
		</p>
	{/if}

	{#if s.peers.length > 0}
		<div class="table-scroll">
			<table class="library">
				<thead>
					<tr>
						<th>Hub peer</th>
						<th>Last sync</th>
						<th>Push fence</th>
						<th>Pull position</th>
						<th>Pending</th>
					</tr>
				</thead>
				<tbody>
					{#each s.peers as peer (peer.peer)}
						<tr>
							<td
								title={`peer machine id ${peer.peer}; generation token ${peer.peer_generation ?? 'unknown'}`}
							>
								{peer.peer.slice(0, 12)}
							</td>
							<td title={peer.last_sync_at ?? 'no sync against this peer has ever completed'}>
								{peer.last_sync_at ?? 'never'}
							</td>
							<td
								title="The greatest local_changelog.seq already offered to this peer. Everything above it is what the next push sends."
							>
								{peer.last_push_seq}
							</td>
							<td title="The greatest hub_changelog.seq already applied from this peer.">
								{peer.last_pull_seq}
							</td>
							<td
								class:warn={peer.pending_push_rows > 0}
								title={peer.full_offer_pending
									? 'This peer has never completed a sync, so the next push is a full offer of the whole library; the changelog count beside it does not describe that.'
									: 'Distinct rows written since this peer last took a push. An upper bound on what the next push offers.'}
							>
								{peer.pending_push_rows}{peer.full_offer_pending ? ' (full offer)' : ''}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	<p class="cli-twin">
		Same bytes as <code>GET /api/v1/cloudsync/overview</code> <code>.sync</code>
		(<code>FleetHealth.to_wire()</code>).
	</p>
</div>

<style>
	.table-scroll {
		overflow-x: auto;
	}
	.sync-health {
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 12px 14px;
		margin-bottom: 1rem;
		background: var(--surface);
	}
	.sync-health.bad {
		border-color: var(--danger);
	}
	.verdict-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 10px;
		font-size: 0.85rem;
	}
	.verdict {
		padding: 3px 10px;
		border-radius: 999px;
		border: 1px solid var(--border);
		font-weight: 600;
		font-size: 0.8rem;
		white-space: nowrap;
	}
	.verdict.ok {
		color: var(--accent);
		border-color: var(--accent);
	}
	.verdict.warn {
		font-weight: 700;
	}
	.verdict.bad {
		color: var(--danger);
		border-color: var(--danger);
	}
	.verdict.muted {
		color: var(--muted);
	}
	dl.readouts {
		display: flex;
		flex-wrap: wrap;
		gap: 18px;
		margin: 12px 0 4px;
	}
	dl.readouts dt {
		color: var(--muted);
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	dl.readouts dd {
		margin: 2px 0 0;
		font-size: 0.95rem;
		font-variant-numeric: tabular-nums;
		cursor: help;
	}
	dl.readouts dd.warn {
		color: var(--danger);
		font-weight: 600;
	}
	.sync-error {
		color: var(--danger);
		font-size: 0.82rem;
		margin: 8px 0 0;
	}
	.cli-twin {
		color: var(--muted);
		font-size: 0.76rem;
		margin: 10px 0 0;
	}
</style>
