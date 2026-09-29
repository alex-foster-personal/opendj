<script lang="ts">
	/**
	 * TopBar "refresh analysis" button (replaces the inert refresh arrow).
	 *
	 * Click: POST /ingest/refresh - finds tracks missing any enabled
	 * ingestion step (analysis / stems / vocals per /ingest/config) and runs
	 * them; a second click while running is a no-op toast (backend 409s).
	 * Hover: popover with the live coverage numbers, the analyze-on-import
	 * queue (locally imported tracks, no rekordbox twin), per-step progress
	 * bar and the job log tail. Polls status at 1Hz while running or hovered.
	 * A failed click opens the same popover (keyboard included); blur or
	 * Escape closes it for a user with no mouse to move away.
	 * As stable_ids complete they get jobProgress badges so the library
	 * updates without a reload.
	 */
	import { onDestroy } from 'svelte';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import {
		getAnalysisQueue,
		getIngestConfig,
		getIngestCoverage,
		getIngestRefreshStatus,
		startIngestRefresh,
		type AnalysisQueue,
		type IngestConfig,
		type IngestCoverage,
		type RefreshStatus
	} from '$lib/rb/api-ingest';
	import { RbApiError } from '$lib/rb/api-rb-error';
	import { jobProgress } from '$lib/rb/job-progress.svelte';
	import { pushToast } from '$lib/stores.svelte';

	let status = $state<RefreshStatus | null>(null);
	let coverage = $state<IngestCoverage | null>(null);
	let config = $state<IngestConfig | null>(null);
	let queue = $state<AnalysisQueue | null>(null);
	let hovered = $state(false);
	let fetchError = $state<string | null>(null);
	let clickFeedback = $state<string | null>(null);
	let wrapEl: HTMLSpanElement | undefined = $state();
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	const badged = new Set<string>();

	const running = $derived(status?.running === true);
	const pct = $derived.by(() => {
		if (status === null || status.step_total === 0) return 0;
		return Math.min(1, status.step_done / status.step_total);
	});

	function _applyBadges(s: RefreshStatus): void {
		if (s.current_step !== 'analysis' && !s.steps_completed.includes('analysis')) return;
		for (const sid of s.recently_done_ids) {
			if (badged.has(sid)) continue;
			badged.add(sid);
			jobProgress.setBadge(sid, 'key', true);
		}
	}

	async function _poll(): Promise<void> {
		try {
			const s = await getIngestRefreshStatus();
			const wasRunning = status?.running === true;
			status = s;
			fetchError = null;
			_applyBadges(s);
			if (wasRunning && !s.running) {
				if (s.phase === 'done') {
					pushToast(`Refresh done: ${s.steps_completed.join(', ')}`, 'info');
				} else if (s.phase === 'error') {
					pushToast(`Refresh failed: ${s.error}`, 'error');
				}
				[coverage, queue] = await Promise.all([getIngestCoverage(), getAnalysisQueue()]);
			}
			_syncTimer();
		} catch (e) {
			fetchError = e instanceof Error ? e.message : String(e);
			_syncTimer();
		}
	}

	function _syncTimer(): void {
		const want = hovered || status?.running === true;
		if (want && pollTimer === null) pollTimer = setInterval(_poll, 1000);
		else if (!want && pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	async function onEnter(): Promise<void> {
		hovered = true;
		_syncTimer();
		void _poll();
		try {
			[coverage, config, queue] = await Promise.all([
				getIngestCoverage(),
				getIngestConfig(),
				getAnalysisQueue()
			]);
			fetchError = null;
		} catch (e) {
			fetchError = e instanceof Error ? e.message : String(e);
		}
	}

	function onLeave(): void {
		hovered = false;
		_syncTimer();
	}

	// A keyboard user opens the popover only through a failed click (below)
	// and has no mouseleave to close it, so leaving the button closes it: focus
	// moving elsewhere, or Escape. The mouse still over it keeps it open.
	function onBlur(): void {
		if (hovered && wrapEl?.matches(':hover') !== true) onLeave();
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape' && hovered) onLeave();
	}

	async function onClick(): Promise<void> {
		clickFeedback = null;
		try {
			status = await startIngestRefresh();
			badged.clear();
			pushToast(`Refresh started: ${status.steps.join(', ')}`, 'info');
			_syncTimer();
		} catch (e) {
			if (e instanceof RbApiError && e.status === 409) {
				clickFeedback = 'Already running';
				pushToast('A refresh is already running', 'info');
			} else if (e instanceof RbApiError && e.status === 422) {
				clickFeedback = 'WIP - not working: no ingestion steps enabled';
				pushToast('No ingestion steps enabled - configure the ingest modal first', 'error');
			} else if (e instanceof RbApiError && e.status === 503) {
				clickFeedback = `WIP - not working: ${e.message}`;
				pushToast(`Refresh unavailable: ${e.message}`, 'error');
			} else {
				clickFeedback = `WIP - not working: ${e instanceof Error ? e.message : String(e)}`;
				pushToast(`Refresh failed to start: ${e instanceof Error ? e.message : e}`, 'error');
			}
			// Open the popover that shows why, as a hover would: poll the status
			// (a 409's running job shows live progress) and fetch the coverage.
			// Activated from the keyboard there was no mouseenter, so nothing
			// else starts them (Codex P2 4130152643). Already open means the
			// mouse's onEnter already ran them; a second call would fetch twice.
			if (!hovered) void onEnter();
		}
	}

	onDestroy(() => {
		if (pollTimer !== null) clearInterval(pollTimer);
	});
</script>

<span
	class="wrap"
	role="presentation"
	bind:this={wrapEl}
	onmouseenter={onEnter}
	onmouseleave={onLeave}
>
	<button
		class="tb-icon"
		class:running
		onclick={onClick}
		onblur={onBlur}
		onkeydown={onKeydown}
		aria-label="refresh analysis"
		data-testid="refresh-analysis"
	>
		<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
			<path d="M9.8 6 a3.8 3.8 0 1 1 -1.1 -2.7" fill="none" stroke="currentColor" stroke-width="1.2" />
			<path d="M9.9 0.8 L9.9 3.6 L7.1 3.6 Z" fill="currentColor" />
		</svg>
	</button>

	{#if hovered}
		<div
			class="pop"
			data-testid="refresh-analysis-pop"
			use:triggerFloatingAction={{ getTrigger: () => wrapEl ?? null, preferred: 'below', gap: 4 }}
		>
			<div class="pop-title">Refresh analysis</div>
			<!-- The latest click's own outcome renders first, whatever the hover
			     fetch or a previous run left behind (Codex P2, PR #3896). -->
			{#if clickFeedback !== null}
				<div class="pop-phase" data-testid="refresh-click-feedback">{clickFeedback}</div>
			{/if}
			{#if fetchError !== null}
				<div class="pop-err">{fetchError}</div>
			{:else}
				{#if coverage !== null}
					<div class="pop-cov" title="tracks with a materialised file missing each artifact; unreachable = broken links, fix via /fix-links">
						missing - analysis {coverage.missing.analysis} · stems {coverage.missing.stems} ·
						vocals {coverage.missing.vocals} · unreachable {coverage.unreachable}
					</div>
				{/if}
				{#if queue !== null}
					<div
						class="pop-queue"
						data-testid="analysis-queue-line"
						title="tracks imported locally with no rekordbox twin; the daemon analyzes these on import so the deck gets tempo, key, energy and auto-cue proposals, and auto is the reconcile loop that drives it. The shipped backend measures no downbeats, so these tracks have no fallback beatgrid"
					>
						local (no rekordbox) - {queue.pending} pending · {queue.analyzed} analyzed ·
						{queue.unreachable} unreachable of {queue.unmapped} · auto
						{queue.auto.enabled ? 'on' : 'off'}
					</div>
				{/if}
				{#if config !== null}
					<div class="pop-steps">
						runs: {config.steps.filter((s) => s.enabled).map((s) => s.id).join(', ') || 'none enabled'}
					</div>
				{/if}
				{#if status !== null && status.phase !== 'idle'}
					<div class="pop-phase">
						{status.phase}{status.current_step !== null ? ` - ${status.current_step}` : ''}
						{#if status.step_total > 0}
							({status.step_done}/{status.step_total})
						{/if}
					</div>
					<div class="bar" title={`${status.step_done}/${status.step_total} items in current step`}>
						<div class="bar-fill" style={`width:${Math.round(pct * 100)}%`}></div>
					</div>
					{#if status.log_tail.length > 0}
						<pre class="pop-log">{status.log_tail.slice(-8).join('\n')}</pre>
					{/if}
				{:else if clickFeedback === null}
					<div class="pop-phase">idle - click to run the enabled steps over every missing track</div>
				{/if}
			{/if}
		</div>
	{/if}
</span>

<style>
	.wrap {
		position: relative;
		display: inline-flex;
	}
	/* visual match for TopBar's .tb-icon without depending on its scoped styles */
	.tb-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 20px;
		height: 18px;
		padding: 0;
		background: none;
		border: none;
		color: #9aa3ad;
		cursor: pointer;
	}
	.tb-icon:hover {
		color: #d7dde3;
	}
	.tb-icon.running {
		color: #4cc9f0;
	}
	.tb-icon.running svg {
		animation: spin 1.2s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	.pop {
		position: fixed;
		z-index: 60;
		width: 340px;
		padding: 8px 10px;
		background: #14171b;
		border: 1px solid #2a2f36;
		border-radius: 4px;
		font-size: 11px;
		color: #c8cfd6;
		box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
	}
	.pop-title {
		font-weight: 600;
		margin-bottom: 4px;
		color: #e8edf2;
	}
	.pop-cov,
	.pop-queue,
	.pop-steps,
	.pop-phase {
		margin-bottom: 4px;
	}
	.pop-err {
		color: #e5484d;
	}
	.bar {
		height: 5px;
		background: #242a31;
		border-radius: 3px;
		overflow: hidden;
		margin-bottom: 5px;
	}
	.bar-fill {
		height: 100%;
		background: #4cc9f0;
		transition: width 400ms linear;
	}
	.pop-log {
		max-height: 110px;
		overflow-y: auto;
		margin: 0;
		padding: 5px 6px;
		background: #0d0f12;
		border-radius: 3px;
		font-size: 10px;
		line-height: 1.35;
		white-space: pre-wrap;
		word-break: break-all;
	}
</style>
