<script lang="ts">
	/**
	 * Props-only half of BackfillQueuePanel (NATIVE-10): the enqueue form, the
	 * last plan, the drain command, the watched batch and recent batches. The
	 * container makes every request and owns the poll; this file draws what it
	 * holds. The form fields are bindable, and the band labels arrive as a
	 * prop because api-analysis-backfill.ts reaches the backend.
	 *
	 * EVERY NUMBER CARRIES A HOVER TITLE (house rule); the parity test reads
	 * this file and the container together.
	 */
	import type {
		BackfillBatchSummary,
		BackfillEnqueueResult,
		BackfillProgress
	} from '$lib/rb/api-analysis-backfill';

	let {
		stableIdsText = $bindable(),
		lane = $bindable(),
		backend = $bindable(),
		note = $bindable(),
		parsedIds,
		batches,
		watched,
		lastPlan,
		fault,
		busy,
		drainCommand,
		bandLabels,
		onEnqueue,
		onCancel,
		onResume,
		refreshWatched,
		watch
	}: {
		stableIdsText: string;
		lane: string;
		backend: string;
		note: string;
		parsedIds: string[];
		batches: BackfillBatchSummary[] | null;
		watched: BackfillProgress | null;
		lastPlan: BackfillEnqueueResult | null;
		/** The last failure, verbatim. */
		fault: string | null;
		busy: boolean;
		drainCommand: string | null;
		bandLabels: Record<BackfillEnqueueResult['band'], string>;
		onEnqueue: () => unknown;
		onCancel: () => unknown;
		onResume: () => unknown;
		refreshWatched: () => unknown;
		watch: (batchId: string) => void;
	} = $props();
</script>

<section class="panel">
	<header>
		<h2>Analysis backfill queue</h2>
		<p class="sub">
			Plan, watch and steer the native-analysis v1 backfill. Concurrency is chosen by the
			memory admission rule from the longest admitted track, never from core count.
		</p>
	</header>

	{#if fault}
		<p class="fault" role="alert">{fault}</p>
	{/if}

	<fieldset>
		<legend>Enqueue</legend>
		<label>
			stable_ids
			<textarea
				bind:value={stableIdsText}
				rows="3"
				placeholder="one per line, or comma separated"
			></textarea>
		</label>
		<div class="row">
			<label>
				lane
				<select bind:value={lane}>
					<option value="beatgrid">beatgrid</option>
					<option value="key">key</option>
					<option value="waveform">waveform</option>
					<option value="loudness">loudness</option>
					<option value="vocal">vocal</option>
				</select>
			</label>
			<label>
				backend
				<input bind:value={backend} />
			</label>
			<label>
				note
				<input bind:value={note} placeholder="why this batch exists" />
			</label>
		</div>
		<p>
			<span class="num" title="How many stable_ids the box currently holds. This is what will be OFFERED to the admission rule, not what will be admitted.">{parsedIds.length}</span>
			offered
			<button onclick={onEnqueue} disabled={busy || parsedIds.length === 0}>Enqueue</button>
		</p>
	</fieldset>

	{#if lastPlan}
		<div class="plan">
			<h3>Plan for {lastPlan.batch_id}</h3>
			<p>
				<span class="num" title="Tracks the memory admission rule accepted: predicted peak RSS (floor + slope x audio-minutes) fits the 6.5 GB per-worker cap and the track is under 90 minutes.">{lastPlan.admitted}</span>
				admitted,
				<span class="num" title="Tracks refused, each with a named reason in the item list below. Refused tracks are NEVER folded into a coverage percentage.">{lastPlan.refused}</span>
				refused,
				<span class="num" title="Worker processes this batch will run at, chosen from the LONGEST ADMITTED track: 4 under 20 min, 2 from 20 to 45 min, 1 above 45 min. Never from core count.">{lastPlan.workers}</span>
				workers
			</p>
			<p class="model" title="The measured peak-RSS model this batch was budgeted under. Floor and slope are measurements of one producer version, not constants.">
				band <code>{lastPlan.band}</code> ({bandLabels[lastPlan.band]}); budgeted at
				<span class="num" title="Predicted peak RSS at zero audio length, in MB, for this producer.">{lastPlan.memory_model.floor_mb}</span>
				MB +
				<span class="num" title="Extra predicted peak RSS per audio minute, in MB, for this producer.">{lastPlan.memory_model.slope_mb_per_min}</span>
				MB/min, measured: {lastPlan.memory_model.measured_on}
			</p>
		</div>
	{/if}

	{#if drainCommand}
		<p class="drain">
			Start the drain (the panel never runs it: a drain holds a process pool for minutes to
			hours):
			<code>{drainCommand}</code>
		</p>
	{/if}

	{#if watched}
		<div class="watched">
			<h3>
				{watched.batch_id} <code>{watched.state}</code>
			</h3>
			<p>
				<span class="num" title="Items in a terminal state: done, skipped, failed or refused. A resume never revisits these.">{watched.settled}</span>
				of
				<span class="num" title="Every item in the batch, admitted and refused alike.">{watched.total}</span>
				settled;
				<span class="num" title="Worker processes the admission rule sized this batch at.">{watched.workers}</span>
				workers, band <code>{watched.band}</code>
			</p>
			<ul class="counts">
				{#each Object.entries(watched.counts) as [state, count] (state)}
					<li>
						{state}
						<span class="num" title={`Items currently in the ${state} state.`}>{count}</span>
					</li>
				{/each}
			</ul>
			<div class="row">
				<button onclick={onCancel} disabled={busy}>Cancel</button>
				<button onclick={onResume} disabled={busy}>Resume</button>
				<button onclick={refreshWatched} disabled={busy}>Refresh</button>
			</div>
			<table>
				<thead>
					<tr><th>track</th><th>lane</th><th>state</th><th>attempts</th><th>reason</th></tr>
				</thead>
				<tbody>
					{#each watched.items as item (item.stable_id + item.lane)}
						<tr>
							<td><code>{item.stable_id}</code></td>
							<td>{item.lane}</td>
							<td>{item.state}</td>
							<td>
								<span class="num" title="How many times a runner has claimed this item. A kill-and-resume shows 2.">{item.attempts}</span>
							</td>
							<td class="reason">{item.reason ?? ''}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	<div class="batches">
		<h3>Recent batches</h3>
		{#if batches === null}
			<p>not loaded</p>
		{:else if batches.length === 0}
			<p>no batches yet</p>
		{:else}
			<ul>
				{#each batches as b (b.batch_id)}
					<li>
						<button onclick={() => watch(b.batch_id)}>{b.batch_id}</button>
						<code>{b.state}</code>
						<span class="num" title="Worker processes the admission rule sized this batch at.">{b.workers}</span>
						workers, {b.created_at}{b.note ? ` -- ${b.note}` : ''}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</section>

<style>
	.panel {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		padding: 1rem;
		font-size: 0.9rem;
	}
	.sub {
		color: var(--text-muted, #888);
		margin: 0.25rem 0 0;
	}
	.fault {
		border: 1px solid #b00;
		padding: 0.5rem;
		color: #b00;
		white-space: pre-wrap;
	}
	fieldset {
		border: 1px solid var(--border, #444);
		padding: 0.75rem;
	}
	.row {
		display: flex;
		gap: 0.75rem;
		flex-wrap: wrap;
		align-items: flex-end;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	textarea,
	input,
	select {
		font-family: inherit;
	}
	.num {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
		cursor: help;
		border-bottom: 1px dotted currentColor;
	}
	.counts {
		display: flex;
		gap: 0.75rem;
		flex-wrap: wrap;
		list-style: none;
		padding: 0;
	}
	table {
		border-collapse: collapse;
		width: 100%;
	}
	th,
	td {
		border-bottom: 1px solid var(--border, #333);
		text-align: left;
		padding: 0.2rem 0.4rem;
	}
	.reason {
		font-size: 0.8rem;
		color: var(--text-muted, #999);
	}
	.batches ul {
		list-style: none;
		padding: 0;
	}
</style>
