<script module lang="ts">
	import { getIngestConfig, onIngestConfigWrite, type IngestConfig } from '$lib/rb/api-ingest';

	/** Shared across every AnalysisDotsPopover instance (one per row in a
	 * virtualized column - potentially hundreds), so scanning down the
	 * column does not fire one ingest-config GET per row hover. Memoized
	 * with a short TTL rather than fetched once forever, since the config
	 * can change (TopBar's ingest modal) while the table stays mounted -
	 * and a write is pushed in immediately below rather than relying on the
	 * TTL alone, so enabling a step elsewhere cannot leave an already-cached
	 * row reporting "disabled" for up to 15s after it stopped being true. */
	const CONFIG_TTL_MS = 15_000;
	let _configCache: IngestConfig | null = null;
	let _configFetchedAt = 0;
	let _configPromise: Promise<IngestConfig> | null = null;

	onIngestConfigWrite((cfg) => {
		_configCache = cfg;
		_configFetchedAt = Date.now();
	});

	async function _sharedIngestConfig(): Promise<IngestConfig> {
		if (_configCache !== null && Date.now() - _configFetchedAt < CONFIG_TTL_MS) {
			return _configCache;
		}
		if (_configPromise === null) {
			_configPromise = getIngestConfig()
				.then((cfg) => {
					_configCache = cfg;
					_configFetchedAt = Date.now();
					return cfg;
				})
				.finally(() => {
					_configPromise = null;
				});
		}
		return _configPromise;
	}
</script>

<script lang="ts">
	/**
	 * Live hover popover for AnalysisDots.svelte (pin a2bb92d16f4a / issue
	 * #880), the maintainer: "analyses grid should have custom hover showing which are
	 * done vs not. Should be able to click those missing to order them, and
	 * show 'in-progress or queued' in the hover modal. the color dots should
	 * replicate in the hover list. Err should do the same."
	 *
	 * DELIBERATELY NOT STORIED (no co-located .stories.ts), the same split
	 * PerfMeters.svelte already establishes: this reaches for the live
	 * jobProgress facade and calls the real track-order API, so it
	 * cannot be a Storybook args-only render (storybook-stories.test.mjs
	 * enforces exactly this line - see AnalysisDots.svelte's own doc
	 * comment). AnalysisDots.svelte itself stays pure/storied; this wraps it.
	 *
	 * The popover and an agent use the same typed track-order endpoint. It
	 * records the requested stable ID and analysis kind in the shared job,
	 * so an order made through either surface has the same live state here.
	 *
	 * FAIL-FAST FIX (bot review, PR #1291, 2 blocking P1s - same bug twice):
	 * the first cut called startIngestRefresh() with no step selection at
	 * all, then UNCONDITIONALLY marked the clicked kind as queued and told
	 * the user so - regardless of whether that kind's step was actually
	 * enabled in the ingest config or actually ran. Clicking "vocals" while
	 * vocals was disabled reported a fabricated success for work that was
	 * never requested, worse than a masked failure. There is no backend
	 * contract to queue one named step (`RefreshIn` in ingest.py has no
	 * `steps` field - the job always runs whatever is enabled in the
	 * persisted config), so this is fixed on both ends of that gap:
	 *   1. BEFORE offering the click affordance, the clicked kind's mapped
	 *      step's enabled/disabled state is read from the ingest config
	 *      (shared, TTL-cached fetch above) and a row whose step is
	 *      disabled is rendered non-clickable with an honest reason,
	 *      rather than offering a click that cannot do what it implies.
	 *   2. ON click, the kind is marked queued ONLY if the refresh
	 *      response's `steps` list actually includes that kind's mapped
	 *      step - never unconditionally. If it does not (a race against a
	 *      config change), the user is told the queue did NOT include this
	 *      kind, rather than a relabelled-but-still-fabricated toast.
	 */
	import {
		ANALYSIS_DOT_SLOTS,
		ANALYSIS_COLORS,
		ANALYSIS_ISSUE_COLORS,
		ANALYSIS_LABELS,
		analysisStatus,
		jobProgress,
		type AnalysisBadge,
		type AnalysisIssues,
		type AnalysisKind
	} from '$lib/rb/job-progress.svelte';
	import { getTrackAnalysisOrders, orderTrackAnalysis } from '$lib/rb/api-ingest';
	import { runAnalysisOrder } from '$lib/rb/analysis-order';
	import { pushToast } from '$lib/stores.svelte';
	import { triggerFloatingAction } from '$lib/ui/clamp-to-viewport';
	import type { GridFlag } from '$lib/rb/analysis-issues';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import AnalysisDots from './AnalysisDots.svelte';

	let {
		badge = {},
		issues = {},
		mode = 'coverage',
		title,
		stableId = null,
		gridFlag = null,
		ongridflagdismiss
	}: {
		badge?: AnalysisBadge;
		issues?: AnalysisIssues;
		mode?: 'coverage' | 'issues';
		title?: string | undefined;
		stableId?: string | null;
		/** GRIDFLAG-04: the row's beatgrid flag when its grid is flagged
		 * (dismissed or not). Shows the dismiss / restore control. */
		gridFlag?: GridFlag | null;
		/** Persist the dismissal. Absent = the control renders inert. */
		ongridflagdismiss?: (dismissed: boolean) => Promise<void>;
	} = $props();

	/** Small debounce so a fast pointer skim across a virtualized column of
	 * rows does not flash a popover per row - same band as SuggestNextStrip's
	 * CHEVRON_EXPLAIN_DELAY_MS (50ms). */
	const HOVER_DELAY_MS = 50;
	let hovered = $state(false);
	let hoverTimer: ReturnType<typeof setTimeout> | null = null;
	let wrapEl: HTMLSpanElement | undefined = $state();
	let ordering = $state<AnalysisKind | null>(null);

	/** Whether the shared ingest config has been read for THIS popover's
	 * lifetime yet, and what it said - kept distinct from "loaded but every
	 * step disabled" so a row can say "checking..." only until this settles
	 * once, honestly, rather than forever guessing. */
	let ingestConfigStatus = $state<'unknown' | 'loaded' | 'error'>('unknown');
	let ingestConfig = $state<IngestConfig | null>(null);
	// When THIS instance's own snapshot was taken - separate from the
	// shared cache's _configFetchedAt, since an already-mounted row must
	// still revalidate periodically rather than trusting its first read
	// forever (Sol review, PRRT_kwDOSEvNd86fkmIf).
	let _instanceConfigFetchedAt = 0;

	const KINDS = ANALYSIS_DOT_SLOTS.filter((k): k is AnalysisKind => k !== null);

	type RowState =
		| { text: string; clickable: false }
		| { text: string; clickable: true; kind: AnalysisKind };

	function dotOn(kind: AnalysisKind): boolean {
		return mode === 'issues' ? issues[kind] !== undefined : badge[kind] === true;
	}

	function dotColor(kind: AnalysisKind): string | undefined {
		if (mode === 'issues') {
			const issue = issues[kind];
			return issue === undefined ? undefined : ANALYSIS_ISSUE_COLORS[issue.severity];
		}
		return ANALYSIS_COLORS[kind];
	}

	function jobPhaseFor(kind: AnalysisKind): 'queued' | 'running' | null {
		if (stableId === null) return null;
		const job = jobProgress.jobs[`${kind}:${stableId}`];
		if (job === undefined) return null;
		return job.phase === 'queued' || job.phase === 'running' ? job.phase : null;
	}

	/** The one coarse ingest step that covers this dot kind (api-ingest.ts's
	 * IngestStep.id) - the same mapping `_ingestStepLabel` used to only name
	 * for the toast, now also used to check whether it is actually enabled. */
	function _ingestStepId(kind: AnalysisKind): 'analysis' | 'stems' | 'vocals' {
		if (kind === 'vocals') return 'vocals';
		if (kind === 'stems') return 'stems';
		return 'analysis'; // beatgrid/key/cues/waveform/phrase/loudness/other share the one coarse step
	}

	function _isOrderable(kind: AnalysisKind): boolean {
		return kind === 'vocals' || kind === 'beatgrid' || kind === 'key' || kind === 'stems';
	}

	function _ingestStepLabel(kind: AnalysisKind): string {
		return _ingestStepId(kind);
	}

	/** true/false once the config is known and the step is actually present
	 * in it; null while still unknown (not yet fetched, or the fetch
	 * failed); undefined when the config loaded successfully but does NOT
	 * contain this step at all - backend contract drift, never a synonym
	 * for "disabled". Sol review on #1291 (PRRT_kwDOSEvNd86fkmIi): the
	 * previous `?? false` collapsed a missing entry into "disabled",
	 * reporting a fabricated deliberate-disable message for what is
	 * actually a contract violation. The caller must never treat null OR
	 * undefined as either "enabled" or "disabled". */
	function _stepEnabled(kind: AnalysisKind): boolean | null | undefined {
		if (ingestConfigStatus !== 'loaded' || ingestConfig === null) return null;
		const stepId = _ingestStepId(kind);
		const step = ingestConfig.steps.find((s) => s.id === stepId);
		return step === undefined ? undefined : step.enabled;
	}

	function rowStateFor(kind: AnalysisKind): RowState {
		const phase = jobPhaseFor(kind);
		const status = analysisStatus(badge[kind] === true, phase);
		if (status === 'queued' || status === 'in-progress') return { text: status, clickable: false };
		if (kind === 'load') return { text: 'deck/audio only - not tracked here', clickable: false };
		if (!_isOrderable(kind)) {
			return { text: 'no producer is available for this analysis', clickable: false };
		}

		if (stableId !== null) {
			if (ingestConfigStatus === 'error') {
				return {
					text: 'could not verify ingest config - not offering to queue',
					clickable: false
				};
			}
			const enabled = _stepEnabled(kind);
			if (enabled === false) {
				return {
					text: `${_ingestStepLabel(kind)} disabled in ingest config - enable it to queue`,
					clickable: false
				};
			}
			if (enabled === undefined) {
				// The config loaded but does not contain this step AT ALL -
				// backend contract drift, not a deliberate disable. Stated
				// honestly rather than folded into "disabled" (Sol review,
				// PRRT_kwDOSEvNd86fkmIi).
				return {
					text: `ingest config has no "${_ingestStepId(kind)}" step - not offering to queue`,
					clickable: false
				};
			}
			if (enabled === null) {
				// Config not settled yet (rare - only the instant the popover
				// first opens): do not offer a click affordance until it is
				// known whether this kind's step can actually run.
				return { text: 'checking ingest config…', clickable: false };
			}
		}

		if (mode === 'coverage') {
			if (status === 'done') return { text: status, clickable: false };
			return stableId === null
				? { text: 'missing', clickable: false }
				: { text: 'missing - click to queue', clickable: true, kind };
		}
		const issue = issues[kind];
		if (issue === undefined) {
			if (kind === 'beatgrid' && gridFlag !== null && gridFlag.dismissed) {
				return { text: `flag dismissed - ${gridFlag.message}`, clickable: false };
			}
			return { text: 'no detected issue', clickable: false };
		}
		// Not judged is not an issue to re-queue: say why and stop there.
		if (issue.severity === 'unknown') return { text: issue.detail, clickable: false };
		return stableId === null
			? { text: `${issue.severity}: ${issue.detail}`, clickable: false }
			: { text: `${issue.severity}: ${issue.detail} - click to re-queue`, clickable: true, kind };
	}

	async function order(kind: AnalysisKind): Promise<void> {
		if (stableId === null || ordering !== null) return;
		if (_stepEnabled(kind) !== true) return; // mirrors rowStateFor's gate - never fire on an unconfirmed step
		ordering = kind;
		const sid = stableId;
		await runAnalysisOrder(kind, {
			orderTrackAnalysis: () => orderTrackAnalysis(sid, kind),
			upsertJob: (phase) => jobProgress.upsert({ stable_id: sid, kind, phase }),
			toast: pushToast
		});
		ordering = null;
	}

	/** GRIDFLAG-04: ask the owner to hide or restore the flag; a refusal is said out loud. */
	async function _toggleGridFlag(dismissed: boolean): Promise<void> {
		try {
			await ongridflagdismiss?.(dismissed);
		} catch (error) {
			pushToast(`Beatgrid flag not ${dismissed ? 'dismissed' : 'restored'}: ${String(error)}`, 'error');
		}
	}

	async function refreshOrders(): Promise<void> {
		if (stableId === null) return;
		const sid = stableId;
		for (const order of await getTrackAnalysisOrders(sid)) {
			jobProgress.upsert({ stable_id: sid, kind: order.kind as AnalysisKind, phase: order.phase });
		}
	}

	function _ensureIngestConfig(): void {
		// Re-check on every open (hover-in/focus-in), not just once per
		// mount: a row already mounted before a step was disabled elsewhere
		// must not keep serving its first snapshot forever, and a prior
		// fetch failure must not be terminal - both were the Sol review
		// finding on #1291 (PRRT_kwDOSEvNd86fkmIf). "Fresh enough" mirrors
		// the shared cache's own TTL, so an instance re-fetches at the same
		// cadence the cache itself would return new data on.
		const stale =
			ingestConfigStatus === 'unknown' ||
			ingestConfigStatus === 'error' ||
			Date.now() - _instanceConfigFetchedAt >= CONFIG_TTL_MS;
		if (!stale) return;
		_sharedIngestConfig()
			.then((cfg) => {
				ingestConfig = cfg;
				ingestConfigStatus = 'loaded';
				_instanceConfigFetchedAt = Date.now();
			})
			.catch(() => {
				ingestConfigStatus = 'error';
			});
	}

	function _openNow(): void {
		if (!wrapEl) return;
		wrapEl.getBoundingClientRect();
		hovered = true;
		_ensureIngestConfig();
		void refreshOrders();
	}

	function onEnter(): void {
		if (hoverTimer !== null) clearTimeout(hoverTimer);
		hoverTimer = setTimeout(_openNow, HOVER_DELAY_MS);
	}

	function onLeave(): void {
		if (hoverTimer !== null) {
			clearTimeout(hoverTimer);
			hoverTimer = null;
		}
		hovered = false;
	}

	/** Keyboard/touch reachability (bot review P2, PR #1291): a hover-only
	 * popover containing real, focusable <button> rows is unreachable for
	 * anyone not using a mouse, and role="tooltip" on a container with
	 * focusable content is itself an ARIA violation (a tooltip must never
	 * contain interactive content). Fixed by making the wrapper focusable
	 * and opening/closing on focus the same as on hover, and dropping
	 * role="tooltip" entirely - the popover is now a plain, unroled
	 * disclosure panel holding a heading and real buttons, which needs no
	 * extra ARIA to be reachable: Tab reaches the wrapper, opens it
	 * immediately (no debounce - a keyboard user is not "skimming"), Tab
	 * again reaches the row buttons, Enter/Space activates one via native
	 * button semantics, Escape closes and returns focus to the wrapper. */
	function onFocusIn(): void {
		if (hoverTimer !== null) {
			clearTimeout(hoverTimer);
			hoverTimer = null;
		}
		_openNow();
	}

	function onFocusOut(e: FocusEvent): void {
		const next = e.relatedTarget as Node | null;
		if (next !== null && wrapEl?.contains(next)) return; // moved to a child row button - stay open
		hovered = false;
	}

	function onKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape') {
			hovered = false;
			wrapEl?.focus();
		}
	}
</script>

<span
	class="wrap"
	role="group"
	aria-label={mode === 'issues' ? 'Data-quality issues' : 'Analysis coverage'}
	data-custom-tip=""
	tabindex="0"
	bind:this={wrapEl}
	onmouseenter={onEnter}
	onmouseleave={onLeave}
	onfocusin={onFocusIn}
	onfocusout={onFocusOut}
	onkeydown={onKeydown}
>
	<AnalysisDots {badge} {issues} {mode} {...(title !== undefined ? { title } : {})} suppressDotTitles={true} />

	{#if hovered}
		<div
			class="pop"
			data-testid="analysis-dots-pop"
			data-hover-card=""
			use:triggerFloatingAction={{ getTrigger: () => wrapEl ?? null, preferred: 'below', gap: 4 }}
		>
			<div class="pop-title">{mode === 'issues' ? 'Data-quality issues' : 'Analysis coverage'}</div>
			<ul class="pop-list">
				{#each KINDS as kind (kind)}
					{@const row = rowStateFor(kind)}
					<li>
						<button
							type="button"
							class="pop-row"
							class:clickable={row.clickable}
							disabled={!row.clickable || ordering !== null}
							onclick={() => row.clickable && void order(kind)}
						>
							<span
								class="pop-dot"
								style={dotOn(kind) ? `--dot:${dotColor(kind)}` : undefined}
								class:on={dotOn(kind)}
								class:unknown={mode === 'issues' && issues[kind]?.severity === 'unknown'}
							></span>
							<span class="pop-label">{ANALYSIS_LABELS[kind]}</span>
							<span class="pop-status" title={row.text}>{ordering === kind ? 'queuing…' : row.text}</span>
						</button>
					</li>
				{/each}
			</ul>
			{#if mode === 'issues' && gridFlag !== null}
				<div class="pop-flag">
					<div class="pop-flag-text" title={gridFlag.message}>{gridFlag.message}</div>
					<button
						type="button"
						class="pop-flag-btn"
						data-testid="grid-flag-dismiss"
						disabled={ongridflagdismiss === undefined}
						title={ongridflagdismiss === undefined
							? plannedTitle('grid-flag-dismiss')
							: gridFlag.dismissed
								? 'Show this beatgrid flag again for this track'
								: 'Hide this beatgrid flag for this track. The grid is not changed, and you can restore the flag here.'}
						onclick={() => void _toggleGridFlag(!gridFlag.dismissed)}
					>
						{gridFlag.dismissed ? 'Restore beatgrid flag' : 'Dismiss beatgrid flag'}
					</button>
				</div>
			{/if}
			{#if stableId === null}
				<div class="pop-note">No track for this row - ordering is unavailable.</div>
			{/if}
		</div>
	{/if}
</span>

<style>
	.wrap {
		position: relative;
		display: inline-block;
	}
	.wrap:focus-visible {
		outline: 1px solid var(--rb-accent, #3d7dd9);
		outline-offset: 1px;
	}
	.pop {
		position: fixed;
		z-index: 9600;
		min-width: 220px;
		background: var(--rb-panel, #14171d);
		border: 1px solid var(--rb-border, #2a3038);
		border-radius: 4px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
		color: var(--rb-text, #c8cdd2);
		font-size: 11px;
		padding: 6px;
	}
	.pop-title {
		font-weight: 600;
		padding: 2px 4px 4px;
		border-bottom: 1px solid var(--rb-border, #2a3038);
		margin-bottom: 4px;
	}
	.pop-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.pop-row {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		padding: 3px 4px;
		border: none;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: default;
		border-radius: 3px;
	}
	.pop-row.clickable {
		cursor: pointer;
	}
	.pop-row.clickable:hover:not(:disabled) {
		background: color-mix(in srgb, var(--rb-accent, #3d7dd9) 18%, transparent);
	}
	.pop-row:disabled {
		opacity: 0.9;
	}
	.pop-dot {
		width: 6px;
		height: 6px;
		border-radius: 1px;
		flex: none;
		background: color-mix(in srgb, var(--rb-text, #888) 18%, transparent);
	}
	.pop-dot.on {
		background: var(--dot);
	}
	.pop-dot.on.unknown {
		background: transparent;
		box-shadow: inset 0 0 0 1px var(--dot);
	}
	.pop-flag {
		margin-top: 4px;
		padding: 4px;
		border-top: 1px solid var(--rb-border, #2a3038);
		max-width: 320px;
	}
	.pop-flag-text {
		color: var(--rb-text-dim, #9aa4b2);
		white-space: normal;
		margin-bottom: 4px;
	}
	.pop-flag-btn {
		font: inherit;
		color: inherit;
		background: transparent;
		border: 1px solid var(--rb-border, #2a3038);
		border-radius: 3px;
		padding: 2px 6px;
		cursor: pointer;
	}
	.pop-flag-btn:hover:not(:disabled) {
		background: color-mix(in srgb, var(--rb-accent, #3d7dd9) 18%, transparent);
	}
	.pop-flag-btn:disabled {
		cursor: default;
		opacity: 0.6;
	}
	.pop-label {
		flex: none;
		width: 60px;
	}
	.pop-status {
		flex: 1;
		color: var(--rb-text-dim, #9aa4b2);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pop-note {
		padding: 4px;
		color: var(--rb-text-dim, #9aa4b2);
	}
</style>
