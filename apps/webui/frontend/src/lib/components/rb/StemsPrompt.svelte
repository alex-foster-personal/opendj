<!--
	The install-flow question: "separate stems for your library?"

	MOUNTED BY THE SETUP WIZARD, not by this lane. The wizard step lives on
	another branch (af--setup-flow); this is the component it mounts and the
	only contract between us is the props below. Nothing here knows it is in a
	wizard, so it is equally droppable into settings later.

	It refuses to ask the question until it can answer the three follow-ups a
	tester will actually have: how many tracks, how long, how much. Those come
	from GET /api/v1/stems/plan, which is the same arithmetic the job uses, so
	the prompt cannot promise a batch the run then disagrees with.

	Agent-native parity: everything on screen here is one GET and one POST, so
	an agent drives the identical flow with no pixels involved. See
	`$lib/rb/stems-jobs.svelte`.
-->
<script lang="ts">
	import { jobsRefusal } from '$lib/api/capabilities.svelte';
	import {
		DEFAULT_STEMS_TIER,
		type StemsPlan,
		type StemsTier,
		enqueueStemsForPending,
		fetchStemsPlan,
		stemsPlanSummary
	} from '$lib/rb/stems-jobs.svelte';

	interface Props {
		/** Quality rung to offer. Defaults to the library default, tier M. */
		tier?: StemsTier;
		/** Called once separation is enqueued, with the new job id. */
		onenqueued?: (jobId: string) => void;
		/** Called when the tester declines. The wizard decides what next. */
		onskip?: () => void;
	}

	const { tier = DEFAULT_STEMS_TIER, onenqueued, onskip }: Props = $props();

	/** A plan fetch that never settles must become a stated failure, not a
	 * sentence that sits on screen forever. The setup shell states the same
	 * rule for its probe screen: no blank window, no endless spinner. */
	const PLAN_TIMEOUT_MS = 20_000;

	let plan = $state<StemsPlan | null>(null);
	let loadError = $state<string | null>(null);
	let enqueueError = $state<string | null>(null);
	let busy = $state(false);
	let enqueuedJobId = $state<string | null>(null);

	const refusal = $derived(jobsRefusal());
	/**
	 * Why separation cannot be started, or null when it can.
	 *
	 * Two independent reasons, and the daemon one comes first because a legacy
	 * boot has no jobs API to enqueue into at all. The second is the build's
	 * GPU transport, reported by the plan endpoint from the relay's own
	 * preflight - on a build with no relay base and no identity token, this is
	 * the honest "stems are not available in this build".
	 */
	const blocked = $derived(
		refusal ?? plan?.local_refusal ?? plan?.transport_refusal ?? null
	);
	const localExecutor = $derived(plan?.executor === 'local');

	$effect(() => {
		if (refusal !== null) return;
		let cancelled = false;
		const timer = setTimeout(() => {
			if (!cancelled && plan === null && loadError === null) {
				loadError = `GET /api/v1/stems/plan did not answer within ${PLAN_TIMEOUT_MS / 1000}s`;
			}
		}, PLAN_TIMEOUT_MS);
		void (async () => {
			try {
				const next = await fetchStemsPlan(tier);
				if (!cancelled) plan = next;
			} catch (exc) {
				// The plan is the whole basis for asking. Without it the prompt
				// says why it cannot ask rather than offering a button whose
				// cost nobody can see.
				if (!cancelled) loadError = exc instanceof Error ? exc.message : String(exc);
			}
		})();
		return () => {
			cancelled = true;
			clearTimeout(timer);
		};
	});

	async function accept(): Promise<void> {
		// The button that reaches here is disabled whenever `blocked` is set;
		// this refuses rather than trusting that, because the cost of being
		// wrong is a queued job that dies on a credential the tester has not
		// got, reported as success.
		if (blocked !== null) {
			enqueueError = `refusing to start separation: ${blocked}`;
			return;
		}
		busy = true;
		enqueueError = null;
		try {
			const job = await enqueueStemsForPending(tier);
			enqueuedJobId = job.id;
			onenqueued?.(job.id);
		} catch (exc) {
			enqueueError = exc instanceof Error ? exc.message : String(exc);
		} finally {
			busy = false;
		}
	}
</script>

<section class="stems-prompt" aria-label="Stem separation">
	<h3>Separate stems?</h3>

	{#if refusal !== null}
		<p class="stems-error" role="alert" title={refusal}>{refusal}</p>
	{:else if loadError !== null}
		<p class="stems-error" role="alert" title={loadError}>
			Could not work out what this would cost, so nothing is being offered yet: {loadError}
		</p>
	{:else if plan === null}
		<p class="stems-note" role="status">Working out how many tracks need stems...</p>
	{:else if blocked !== null && plan !== null}
		<!--
			STEMS CANNOT START ON THIS BUILD. The counts are still shown so the
			tester sees how much work exists behind the refusal. The button stays
			visible but inert with the server's own tooltip (PERFMODE tier floor,
			feature flag, or farm transport), never a silent no-op.
		-->
		<p class="stems-error" role="alert" title={blocked}>
			{localExecutor
				? `Local stems are not available on this machine: ${blocked}`
				: `Stems are not available in this build: ${blocked}`}
		</p>
		<p class="stems-note" title={stemsPlanSummary(plan)}>
			{plan.pending} of {plan.total} tracks would need separating. Nothing is queued
			{localExecutor ? '.' : ' and nothing is charged.'}
		</p>
		<div class="stems-actions">
			<button class="stems-go rb-inert" disabled title={blocked}>
				Separate {plan.pending} tracks
			</button>
			<button class="stems-skip" onclick={() => onskip?.()}>Continue without stems</button>
		</div>
	{:else if enqueuedJobId !== null}
		<p class="stems-note" title={`Engine job ${enqueuedJobId}`}>
			Started. Tracks gain their stems as each one finishes; the bar at the top of the
			menu shows how far along it is.
		</p>
	{:else if plan.pending === 0}
		<p
			class="stems-note"
			title={`${plan.ready} of ${plan.total} library rows already have a stem bundle on disk; ${plan.unavailable} have no audio on this machine.`}
		>
			Every track that has audio here already has stems. Nothing to do.
		</p>
	{:else}
		<p class="stems-summary" title={stemsPlanSummary(plan)}>{stemsPlanSummary(plan)}</p>
		<p class="stems-note">
			{localExecutor
				? 'Separation runs on this machine in the background at low priority; you can keep using the app.'
				: 'Separation runs on rented GPUs and is billed to the account this build is configured with. It runs in the background; you can keep using the app.'}
		</p>
		<div class="stems-actions">
			<button class="stems-go" disabled={busy} onclick={accept}>
				{busy ? 'Starting...' : `Separate ${plan.pending} tracks`}
			</button>
			<button class="stems-skip" disabled={busy} onclick={() => onskip?.()}>
				Not now
			</button>
		</div>
		{#if enqueueError !== null}
			<p class="stems-error" title={enqueueError}>Could not start: {enqueueError}</p>
		{/if}
	{/if}
</section>

<style>
	.stems-prompt {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 12px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
	}
	h3 {
		margin: 0;
		font-size: 13px;
		letter-spacing: 0.04em;
	}
	.stems-summary {
		margin: 0;
		font-size: 12px;
	}
	.stems-note {
		margin: 0;
		font-size: 11px;
		color: var(--rb-text-dim);
	}
	.stems-error {
		margin: 0;
		font-size: 11px;
		/* --rb-danger was never defined anywhere, so every message in this
		   class silently rendered in the hardcoded fallback rather than the
		   palette. --rb-red is the palette's real danger colour (theme.css),
		   and this component mounts inside .perf-root where it resolves. */
		color: var(--rb-red);
		font-weight: 560;
	}
	.stems-actions {
		display: flex;
		gap: 8px;
	}
	button {
		font: inherit;
		font-size: 11px;
		padding: 4px 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		cursor: pointer;
	}
	.stems-go {
		border-color: var(--rb-accent);
	}
	button:disabled {
		cursor: default;
		opacity: 0.6;
	}
</style>
