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
	 * Every click opens the same popover (keyboard included); blur or
	 * Escape closes it for a user with no mouse to move away.
	 * As stable_ids complete they get jobProgress badges so the library
	 * updates without a reload.
	 *
	 * The popover markup lives in RefreshAnalysisPopover.svelte and is fetched
	 * by the first open (hover or click), the pattern MidiPanelLoader and
	 * TrackRowPopovers use: it only exists after that, so it stays out of the
	 * /performance initial chunks. This file keeps the button, the polling and
	 * all state, and passes what the popover shows down as props.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ The popover module is not imported until `hovered` is first true.
	 *     [if] any module statically imports RefreshAnalysisPopover [then] ⛔️
	 *   ✔︎ The popover renders only while `hovered` holds, so a dismissal
	 *     (mouseleave, blur, Escape) while the module is still loading stays
	 *     made when it arrives.
	 *     [if] the popover renders on load without checking `hovered` [then] ⛔️
	 *   ✔︎ A failed fetch is shown inline, where the popover would be, with the
	 *     error text and a Close, never a silent nothing.
	 *     [if] the import rejects and the hover shows nothing [then] ⛔️
	 *   ✔︎ A failed fetch is recovered by a fresh document on the user's click
	 *     (Reload), never by re-importing in this one: the browser keeps the
	 *     failed fetch in its module map (see MidiPanelLoader), so the popover
	 *     is fetched once per document and a later open shows the same error.
	 *     [if] the error promises a retry on the next open, or reloads unasked [then] ⛔️
	 */
	import { onDestroy, type Component } from 'svelte';
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
	import { refreshSnapshotApplies } from '$lib/rb/refresh-status-order';
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

	type PopoverProps = {
		wrapEl: HTMLSpanElement | undefined;
		status: RefreshStatus | null;
		coverage: IngestCoverage | null;
		config: IngestConfig | null;
		queue: AnalysisQueue | null;
		fetchError: string | null;
		clickFeedback: string | null;
	};
	let Popover: Component<PopoverProps> | null = $state(null);
	let popoverLoadError: string | null = $state(null);
	let popoverRequested = false;

	$effect(() => {
		if (!hovered || popoverRequested) return;
		popoverRequested = true;
		import('./RefreshAnalysisPopover.svelte')
			.then((m) => {
				Popover = m.default;
			})
			.catch((exc: unknown) => {
				console.error('[refresh-analysis] popover failed to load', exc);
				popoverLoadError = exc instanceof Error ? exc.message : String(exc);
			});
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
		// Still the shown snapshot when the answer lands = asked after it arrived.
		const asked = status;
		try {
			const s = await getIngestRefreshStatus();
			// Out-of-order answers and a restarted backend: refresh-status-order.ts.
			if (!refreshSnapshotApplies(status, s, asked === status)) return;
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

	// A keyboard user opens the popover only by clicking (below) and has no
	// mouseleave to close it, so leaving the button closes it: focus
	// moving elsewhere, or Escape. The mouse still over it keeps it open.
	function onBlur(): void {
		if (hovered && !wrapEl?.matches(':hover')) onLeave();
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape' && hovered) onLeave();
	}

	async function onClick(): Promise<void> {
		clickFeedback = null;
		// Every click, started or refused, opens the popover as a hover would:
		// it polls the status (the run's live progress) and fetches coverage.
		// From the keyboard there was no mouseenter, so nothing else starts them
		// (Codex P2 4130152643, 4130407830). Opened BEFORE the POST, so a blur,
		// Escape or mouseleave while it is pending stays dismissed (4130581613).
		// Already open means the mouse's onEnter ran them and its timer is live.
		if (!hovered) void onEnter();
		try {
			status = await startIngestRefresh();
			badged.clear();
			pushToast(`Refresh started: ${status.steps.join(', ')}`, 'info');
			// Poll while the run lasts even if the popover was dismissed while
			// the POST was pending: the badges and the done toast come from it.
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

	{#if hovered && Popover !== null}
		<Popover {wrapEl} {status} {coverage} {config} {queue} {fetchError} {clickFeedback} />
	{:else if hovered && popoverLoadError !== null}
		<div
			class="pop-load-error"
			role="alert"
			data-testid="refresh-analysis-pop-load-error"
			use:triggerFloatingAction={{ getTrigger: () => wrapEl ?? null, preferred: 'below', gap: 4 }}
		>
			<span>Refresh analysis popover failed to load: {popoverLoadError}</span>
			<button type="button" onclick={() => location.reload()}>Reload</button>
			<button type="button" onclick={onLeave}>Close</button>
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
	.pop-load-error {
		position: fixed;
		z-index: 60;
		display: flex;
		gap: 8px;
		align-items: center;
		max-width: 340px;
		padding: 6px 10px;
		background: #14171b;
		border: 1px solid #e5484d;
		border-radius: 4px;
		color: #e5484d;
		font-size: 11px;
		overflow-wrap: anywhere;
	}
	.pop-load-error button {
		background: transparent;
		border: 1px solid #2a2f36;
		color: #c8cfd6;
		font-size: 11px;
		cursor: pointer;
	}
</style>
