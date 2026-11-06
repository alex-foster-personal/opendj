<script lang="ts">
	// "Why this track" and the one-tap verdict (ADR-0139). Pure logging: reads GET /dj/transitions/last, posts the verdict.
	import {
		VERDICTS,
		chosenCandidate,
		fetchLastDecision,
		verdictOffered,
		type Verdict,
		type WhyRecord
	} from '$lib/rb/autoplay-why';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { autoPlayDecision } from '$lib/rb/auto-play-early.svelte';

	let open = $state(false);
	let record = $state<WhyRecord | null>(null);
	let error = $state<string | null>(null);

	async function refresh(): Promise<void> {
		try {
			record = await fetchLastDecision();
			error = null;
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		}
	}

	async function judge(verdict: Verdict): Promise<void> {
		if (record === null) return;
		// The typed dispatcher, so agents drive the same control (AGENTS.md parity); HTTP and CLI are its twins.
		const result = await runPerformanceCommandFromUi({
			type: 'autoplay_verdict',
			out: record.out_track_id,
			in: record.in_track_id,
			verdict
		});
		await refresh();
		// After the refresh, so a failed store stays on screen instead of being cleared by it.
		if (!result.ok) {
			error = `verdict not stored: ${String(result.error)}`;
			open = true;
		}
	}

	// Re-read whenever AutoPlay's decision moves on (a transition ran, a new pair was decided), so the verdict
	// buttons follow the pair that just played rather than the one open when the menu was.
	$effect(() => {
		void autoPlayDecision.phase;
		void autoPlayDecision.track;
		void refresh();
	});

	const chosen = $derived(record === null ? null : chosenCandidate(record));
</script>

<div class="why" data-testid="ap-why">
	<button
		type="button"
		class="ap-row"
		data-testid="ap-why-toggle"
		title="What AutoPlay was offered for its last pick, what it rejected and why, and the chosen transition's warnings"
		onclick={() => {
			open = !open;
			if (open) void refresh();
		}}>Why this track</button
	>
	{#if verdictOffered(record)}
		<div
			class="ap-row verdict"
			data-testid="ap-verdict"
			title="How did the last AutoPlay transition sound? Stored with the pair; it never changes the planner"
		>
			<span>How was that?</span>
			{#each VERDICTS as verdict (verdict)}
				<button
					type="button"
					data-testid={`ap-verdict-${verdict}`}
					onclick={() => void judge(verdict)}>{verdict}</button
				>
			{/each}
		</div>
	{/if}
	{#if open}
		{#if error !== null}
			<p class="ap-status" data-testid="ap-why-error">{error}</p>
		{:else if record === null}
			<p class="ap-status" data-testid="ap-why-empty">No AutoPlay decision stored yet.</p>
		{:else}
			<p class="ap-status" data-testid="ap-why-reason">{record.decision.reason}</p>
			<p
				class="ap-status"
				data-testid="ap-why-intent"
				title="The intent the chooser planned for, and the styles already played that its variety rule weighed"
			>
				Intent: {record.decision.intent ?? 'default (smooth)'}; recent styles: {record.decision
					.recent_styles?.length
					? record.decision.recent_styles.join(', ')
					: 'none'}
			</p>
			<p class="ap-status" title="Tracks AutoPlay's own picker shortlisted, best first">
				Shortlist: <span title="How many tracks were shortlisted"
					>{record.decision.shortlist.length}</span
				>
			</p>
			<ul class="why-list" data-testid="ap-why-candidates">
				{#each record.decision.candidates as candidate (candidate.cid)}
					<li class:chosen={candidate.cid === record.decision.chosen}>
						{candidate.cid === record.decision.chosen ? 'Chosen' : 'Offered'}: {candidate.track.slice(
							0,
							10
						)}
						{candidate.style}
						<span title="Bars of overlap this candidate planned">{candidate.bars} bars</span>,
						<span
							title="The proposal's rule score, 0..1: an eligibility floor, not the reason a style won"
							>score {candidate.score.toFixed(3)}</span
						>
					</li>
				{/each}
				{#each record.decision.rejected as rejected (rejected.track)}
					<li>Rejected: {rejected.track.slice(0, 10)}: {rejected.reason}</li>
				{/each}
			</ul>
			{#if chosen !== null}
				{#each chosen.warnings as warning (warning)}
					<p class="ap-status" data-testid="ap-why-warning">Warning: {warning}</p>
				{/each}
			{/if}
			{#if record.verdict !== null}
				<p class="ap-status" data-testid="ap-why-verdict">Verdict: {record.verdict}</p>
			{/if}
		{/if}
	{/if}
</div>

<style>
	.why-list {
		max-height: 30vh;
		overflow-y: auto;
		margin: 0;
		padding-left: 1rem;
		font-size: 0.75rem;
	}
	.chosen {
		font-weight: 600;
	}
	.verdict {
		display: flex;
		gap: 0.25rem;
		align-items: center;
	}
</style>
