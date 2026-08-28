<script lang="ts">
	/**
	 * Engine jobs drawer (T5): the live list of what the engine is running.
	 *
	 * Every row is real. There is no simulated progress and no placeholder
	 * row: an empty list says "no jobs yet" rather than inventing one, and a
	 * bar only moves when the engine publishes a jobs.updated frame carrying
	 * that row.
	 *
	 * Refusals are shown, not hidden. Cancel is offered only for a running
	 * job because the engine refuses every other status outright, and the
	 * button explains why it is inert. When the engine refuses anyway (the
	 * row moved between render and click) its 409 message is printed
	 * verbatim under the row.
	 */
	import { jobsRefusal } from '$lib/api/capabilities.svelte';
	import {
		cancelRefusal,
		canCancel,
		canReenqueue,
		errorTail,
		formatJobTime,
		jobsStore,
		progressPct,
		reenqueueRefusal,
		type Job
	} from '$lib/rb/jobs-store.svelte';

	const open = $derived(jobsStore.drawerOpen);

	/** Why this drawer is inert, or null when the daemon offers jobs. */
	const refusal = $derived(jobsRefusal());

	/**
	 * Wire to the engine only while the drawer is open AND the daemon serving
	 * this page actually has a jobs API.
	 *
	 * The jobs endpoints and the jobs.updated topic exist on the ENGINE, not
	 * on the legacy daemon that also serves this SPA during the bake-off. The
	 * capability probe has already established which one is answering, so a
	 * legacy boot subscribes to nothing and fetches nothing rather than 404ing
	 * to rediscover it. Attaching on open (and detaching on close, via the
	 * returned teardown) also means reopening refetches instead of showing a
	 * list that went stale while the drawer was shut.
	 */
	$effect(() => {
		if (!jobsStore.drawerOpen) return;
		if (refusal !== null) return;
		return jobsStore.attach();
	});

	function close(): void {
		jobsStore.closeDrawer();
	}

	function statusTitle(job: Job): string {
		const parts = [`status: ${job.status}`, `attempt ${job.attempt}`];
		if (job.owner_boot_id !== '') parts.push(`engine boot ${job.owner_boot_id}`);
		if (job.external_ref !== null && job.external_ref !== undefined) {
			parts.push(`ref ${job.external_ref}`);
		}
		return parts.join(' | ');
	}

	function progressTitle(job: Job): string {
		return `${progressPct(job.progress)}% complete, as last reported by the worker (server value ${job.progress} on a 0..1 scale)`;
	}

	function timeTitle(job: Job): string {
		const created = `created ${job.created_at}`;
		if (job.finished_at !== null && job.finished_at !== undefined) {
			return `${created}, finished ${job.finished_at}`;
		}
		if (job.started_at !== null && job.started_at !== undefined) {
			return `${created}, started ${job.started_at}, not finished`;
		}
		return `${created}, not started`;
	}
</script>

{#if open}
	<div class="jobs-backdrop" role="presentation" onclick={close}></div>
	<aside class="jobs-panel" aria-label="Engine jobs">
		<header class="jobs-head">
			<strong title="Jobs the engine is running. Live over the jobs.updated topic, never polled.">
				Jobs
			</strong>
			<span
				class="jobs-count"
				title="Number of job rows currently loaded (newest first, capped at the server's list limit)"
			>
				{jobsStore.jobs.length}
			</span>
			<button
				type="button"
				class="jobs-refresh"
				onclick={() => jobsStore.hydrate()}
				disabled={jobsStore.loading || refusal !== null}
				title={refusal ?? 'Refetch the list from the engine'}
			>
				{jobsStore.loading ? '...' : 'refresh'}
			</button>
			<button type="button" class="jobs-x" onclick={close} title="Close" aria-label="Close">
				x
			</button>
		</header>

		{#if refusal !== null}
			<!-- INERT: nothing was fetched and nothing is subscribed. -->
			<p class="jobs-inert" title={refusal}>{refusal}</p>
		{:else if jobsStore.error !== null}
			<p class="jobs-error" role="alert" title={jobsStore.error}>
				could not load jobs: {jobsStore.error}
			</p>
		{/if}

		{#if refusal !== null}
			<!-- No list at all, not even an empty one: an empty list would claim
			     the daemon told us it has no jobs, and it never spoke. -->
		{:else if jobsStore.jobs.length === 0}
			<p class="jobs-empty">no jobs yet</p>
		{:else}
			<ul class="jobs-list">
				{#each jobsStore.jobs as job (job.id)}
					<li class="jobs-row">
						<div class="jobs-line">
							<span class="jobs-kind" title={`job kind: ${job.kind} (id ${job.id})`}>
								{job.kind}
							</span>
							<span class={`jobs-chip st-${job.status}`} title={statusTitle(job)}>
								{job.status}
							</span>
							<span class="jobs-time" title={timeTitle(job)}>
								{formatJobTime(job.finished_at ?? job.created_at)}
							</span>
						</div>

						<div
							class="jobs-bar"
							role="progressbar"
							aria-valuenow={progressPct(job.progress)}
							aria-valuemin="0"
							aria-valuemax="100"
							aria-label={`${job.kind} progress`}
							title={progressTitle(job)}
						>
							<div class="jobs-fill" style={`width: ${progressPct(job.progress)}%`}></div>
							<span class="jobs-pct" title={progressTitle(job)}>{progressPct(job.progress)}%</span>
						</div>

						{#if job.message !== null && job.message !== undefined && job.message !== ''}
							<p class="jobs-message" title={job.message}>{job.message}</p>
						{/if}

						{#if job.error !== null && job.error !== undefined && job.error !== ''}
							<pre class="jobs-tail" title={job.error}>{errorTail(job.error)}</pre>
						{/if}

						<div class="jobs-actions">
							<button
								type="button"
								disabled={!canCancel(job) || jobsStore.busyId === job.id}
								title={cancelRefusal(job) ?? 'Stop this job: the engine kills the worker group and confirms it is dead'}
								onclick={() => jobsStore.cancel(job.id)}
							>
								cancel
							</button>
							<button
								type="button"
								disabled={!canReenqueue(job) || jobsStore.busyId === job.id}
								title={reenqueueRefusal(job) ??
									'Queue this job again as a new attempt. An "unknown" row is refused unless a reconcile hook can establish what the last worker did.'}
								onclick={() => jobsStore.reenqueue(job.id)}
							>
								re-enqueue
							</button>
						</div>

						{#if jobsStore.actionError !== null && jobsStore.actionError.id === job.id}
							<p class="jobs-refusal" role="alert" title={jobsStore.actionError.message}>
								{jobsStore.actionError.message}
							</p>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</aside>
{/if}

<style>
	.jobs-backdrop {
		position: absolute;
		inset: 0;
		z-index: 40;
		background: rgba(0, 0, 0, 0.35);
	}
	.jobs-panel {
		position: absolute;
		right: 8px;
		top: calc(var(--rb-topbar-h) + 4px);
		z-index: 41;
		width: min(360px, calc(100% - 16px));
		max-height: min(460px, 70%);
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 10px;
		background: var(--rb-panel-raised, #1a1e25);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 12px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
		overflow: auto;
	}
	.jobs-head {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.jobs-count {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.jobs-refresh,
	.jobs-x {
		margin-left: auto;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 10px;
		padding: 1px 6px;
		cursor: pointer;
	}
	.jobs-x {
		margin-left: 0;
	}
	.jobs-empty {
		color: var(--rb-text-dim);
		margin: 4px 0;
	}
	.jobs-inert {
		color: var(--rb-text-dim);
		margin: 2px 0;
		overflow-wrap: anywhere;
	}
	.jobs-error,
	.jobs-refusal {
		color: var(--rb-red);
		margin: 2px 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.jobs-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.jobs-row {
		border-top: 1px solid var(--rb-border);
		padding-top: 6px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.jobs-line {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.jobs-kind {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.jobs-time {
		margin-left: auto;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.jobs-chip {
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 0 4px;
		font-size: 10px;
		text-transform: uppercase;
		color: var(--rb-text-dim);
	}
	.jobs-chip.st-running,
	.jobs-chip.st-cancelling {
		color: var(--rb-accent);
		border-color: var(--rb-accent);
	}
	.jobs-chip.st-succeeded {
		color: var(--rb-green);
		border-color: var(--rb-green);
	}
	.jobs-chip.st-failed,
	.jobs-chip.st-unknown {
		color: var(--rb-red);
		border-color: var(--rb-red);
	}
	.jobs-bar {
		position: relative;
		height: 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		overflow: hidden;
	}
	.jobs-fill {
		height: 100%;
		background: var(--rb-accent);
	}
	.jobs-pct {
		position: absolute;
		inset: 0;
		text-align: center;
		font-size: 9px;
		line-height: 10px;
		color: var(--rb-text);
		font-variant-numeric: tabular-nums;
	}
	.jobs-message {
		margin: 0;
		color: var(--rb-text-dim);
		overflow-wrap: anywhere;
	}
	.jobs-tail {
		margin: 0;
		padding: 4px;
		background: var(--rb-bg);
		border: 1px solid var(--rb-border);
		color: var(--rb-red);
		font-size: 10px;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		max-height: 60px;
		overflow: auto;
	}
	.jobs-actions {
		display: flex;
		gap: 6px;
	}
	.jobs-actions button {
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 10px;
		padding: 1px 6px;
		cursor: pointer;
	}
	.jobs-actions button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
</style>
