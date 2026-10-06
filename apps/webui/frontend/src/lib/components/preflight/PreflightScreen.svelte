<script lang="ts">
	/**
	 * PREFLIGHT-01's boot gate + admin reference (issue #771).
	 *
	 * mode="boot": mounted once at the app root while the gate is unresolved
	 * (see +layout.svelte). Polls only while the last response was not a
	 * clean pass, and stops the instant it is -- the parent layout then swaps
	 * this out for the real app, which IS the auto-advance; this component
	 * holds no "advance past the gate" logic of its own, only the checks.
	 * mode="admin": mounted in the admin/diagnostics area as a read-only,
	 * always-live-polling reference. Same rows, same data, no navigation
	 * side effect either way -- this component never calls goto().
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ 🎯 boot mode never renders a skip/continue-anyway control -- the
	 *     ONLY way past this screen is a real `pass` from the server.
	 *     [if] a control here bypasses the gate without a pass [then ⛔️] broken
	 *   ✔︎ 🎯 "Re-request permissions" is never dead: it always re-issues the
	 *     real check (which is what makes macOS re-prompt if it never has),
	 *     and the audio-access row's remediation link (System Settings)
	 *     renders whenever the server's remediation text carries one, so a
	 *     prior-denial user always has a next step.
	 *     [if] the settings link is swallowed instead of rendered [then ⛔️] broken
	 *   ✔︎ 🎯 every numeric/status readout carries a `title` explaining itself
	 *     (house rule), not just a color.
	 *     [if] the light or the status word has no title/text fallback [then ⛔️] broken
	 *   ✔︎ 🎯 PREFLIGHT-02 (issue #2589): a `library-attached` row that is
	 *     `pending` because setup was dismissed with an empty library shows a
	 *     real, working "Run setup" control -- a dismissed-empty library must
	 *     never be a dead end with only prose remediation. That row's markup
	 *     lives in PreflightCheckRow.svelte (see its own doc for why it is a
	 *     separate, lifecycle-hook-free component).
	 *     [if] this state renders no control, or a disabled/inert one, while
	 *     the daemon is reachable [then ⛔️] broken
	 *
	 * NAVIGATION IS INJECTED, never imported, exactly for the reason
	 * `run-setup.ts` documents: `$app/navigation` only exists inside a
	 * SvelteKit runtime. Both call sites already import `goto` for their own
	 * use and pass it straight through to PreflightCheckRow.
	 */
	import AlphaBadge from '$lib/components/AlphaBadge.svelte';
	import { onDestroy, onMount } from 'svelte';
	import { OVERLAY_Z } from '$lib/overlays/stack';
	import { bootGateHeading, needsImportAction } from '$lib/preflight/boot-copy';
	import { bootCheckDetail, bootCheckLabel } from '$lib/preflight/preflight-boot-copy';
	import { checkPreflight, preflightGate, requestPermissions } from '$lib/preflight/preflight.svelte';
	import {
		checkOffersRunSetup,
		preflightExplainers,
		runImport,
		runRecheck,
		runRequestPermissions,
		runRetrySetupCheck,
		type PreflightActionId
	} from '$lib/preflight/preflight-actions';
	import { firstRunGate, retryFirstRunGate } from '$lib/setup/first-run-gate.svelte';
	import { runSetup, runSetupBlocked } from '$lib/setup/run-setup';
	import PreflightActions from './PreflightActions.svelte';
	import PreflightCheckRow from './PreflightCheckRow.svelte';

	const POLL_MS = 3_000;

	let {
		mode = 'boot',
		blocking = true,
		hideCheckIds = [],
		navigate
	}: {
		mode?: 'boot' | 'admin';
		blocking?: boolean;
		hideCheckIds?: string[];
		navigate?: (path: string) => unknown;
	} = $props();

	const hiddenIds = $derived(new Set(hideCheckIds));
	const visibleChecks = $derived(
		preflightGate.checks.filter((check) => !hiddenIds.has(check.id))
	);

	let timer: ReturnType<typeof setInterval> | null = null;
	let pendingAction = $state<PreflightActionId | null>(null);
	let actionStatus = $state<string | null>(null);

	const bootHeadline = $derived(
		mode === 'boot' && blocking
			? bootGateHeading(blocking, preflightGate.checks, preflightGate.consecutiveFailPolls)
			: mode === 'boot'
				? 'Startup checks'
				: 'Preflight'
	);

	const showImportCta = $derived(
		mode === 'boot' &&
			blocking &&
			(needsImportAction(preflightGate.checks) ||
				preflightGate.needsActionCopy ||
				firstRunGate.hasError ||
				firstRunGate.isResolving)
	);

	function stopPolling(): void {
		if (timer === null) return;
		clearInterval(timer);
		timer = null;
	}

	async function pollPreflight(): Promise<void> {
		await checkPreflight();
	}

	function ensurePolling(): void {
		if (timer !== null) return;
		timer = setInterval(() => void pollPreflight(), POLL_MS);
	}

	// Boot mode polls only while not yet cleared (a fresh pass stops it, and
	// the parent layout unmounts this component on the same tick). Admin mode
	// polls for as long as it stays mounted, regardless of status -- it is a
	// live reference, not a gate.
	$effect(() => {
		if (mode === 'admin') {
			ensurePolling();
			return;
		}
		if (preflightGate.cleared) {
			stopPolling();
		} else {
			ensurePolling();
		}
	});

	onMount(() => {
		void pollPreflight();
	});

	onDestroy(stopPolling);

	const showSurface = $derived(
		mode === 'admin' ||
			blocking ||
			visibleChecks.length > 0 ||
			preflightGate.error !== null ||
			firstRunGate.hasError
	);

	const visibleActions = $derived<PreflightActionId[]>([
		...(mode === 'boot' && blocking && firstRunGate.hasError ? (['retry-setup'] as const) : []),
		...(showImportCta ? (['import'] as const) : []),
		'recheck',
		'permissions'
	]);
	const disabledActions = $derived<PreflightActionId[]>(
		runSetupBlocked() !== null || !navigate ? ['import'] : []
	);
	const footer = $derived(
		mode !== 'boot' || !blocking
			? null
			: preflightGate.needsActionCopy
				? 'Import your music below to continue, or fix the items above and re-check.'
				: 'This screen clears itself automatically once every check passes.'
	);

	function openImporter(): Promise<string | null> {
		return navigate ? runSetup(navigate) : Promise.resolve('no way to open setup from this screen');
	}

	function setActionStatus(status: string): void {
		actionStatus = status;
	}

	const checkDeps = {
		check: checkPreflight,
		snapshot: () => ({ checks: visibleChecks, error: preflightGate.error }),
		now: () => new Date(),
		labelOf: (check: (typeof preflightGate.checks)[number]) =>
			mode === 'boot' ? bootRowLabel(check) : check.label,
		setStatus: setActionStatus
	};

	/** PREFLIGHT-05: every click shows a pending line, then the real result. */
	async function handleAction(id: PreflightActionId): Promise<void> {
		if (pendingAction !== null) return;
		pendingAction = id;
		try {
			if (id === 'recheck') {
				await runRecheck(checkDeps);
			} else if (id === 'permissions') {
				await runRequestPermissions({ ...checkDeps, check: requestPermissions });
			} else if (id === 'import') {
				await runImport(openImporter, setActionStatus);
			} else if (id === 'retry-setup') {
				await runRetrySetupCheck(
					{ retry: retryFirstRunGate, gateError: () => firstRunGate.error, open: openImporter },
					setActionStatus
				);
			} else {
				const _exhaustive: never = id;
				throw new Error(`Unhandled preflight action: ${_exhaustive}`);
			}
		} finally {
			pendingAction = null;
		}
	}

	function bootRowLabel(check: (typeof preflightGate.checks)[number]): string {
		return bootCheckLabel(check, mode);
	}

	function bootRowDetail(check: (typeof preflightGate.checks)[number]): string {
		return bootCheckDetail(check, mode);
	}
</script>

{#if showSurface}
<section
	class="preflight"
	class:preflight-boot={mode === 'boot' && blocking}
	class:preflight-boot-strip={mode === 'boot' && !blocking}
	class:preflight-admin={mode === 'admin'}
	data-preflight-mode={mode}
	data-preflight-status={preflightGate.status}
	data-preflight-blocking={blocking ? 'true' : 'false'}
	style:--preflight-boot-z={mode === 'boot' && blocking ? OVERLAY_Z.preflightBoot : undefined}
>
	{#if mode === 'boot' && blocking}
		<header class="preflight-brand" aria-label="Open DJ">
			<div class="preflight-mark" aria-hidden="true"></div>
			<div class="preflight-lockup">
				<span class="preflight-name">Open DJ<AlphaBadge /></span>
				<p class="preflight-welcome">
					Welcome. Import your music to start DJing, or keep going once setup finishes.
				</p>
			</div>
		</header>
	{/if}
	<h2>{bootHeadline}</h2>
	{#if preflightGate.error}
		<p class="preflight-error">Could not reach the engine: {preflightGate.error}</p>
	{/if}
	{#if mode === 'boot' && blocking && firstRunGate.hasError}
		<p class="preflight-error" role="alert">{firstRunGate.error}</p>
	{/if}
	<ul class="preflight-checks">
		{#each visibleChecks as check (check.id)}
			<PreflightCheckRow
				{check}
				{navigate}
				{mode}
				label={mode === 'boot' ? bootRowLabel(check) : check.label}
				detail={mode === 'boot' ? bootRowDetail(check) : check.detail}
			/>
		{/each}
	</ul>
	<PreflightActions
		actions={visibleActions}
		disabled={disabledActions}
		pending={pendingAction}
		status={actionStatus}
		{footer}
		explainers={preflightExplainers(
			{ actions: visibleActions, runSetup: visibleChecks.some(checkOffersRunSetup) },
			runSetupBlocked()
		)}
		onAction={(id: PreflightActionId) => void handleAction(id)}
	/>
</section>
{/if}

<style>
	/* No success/positive token exists in app.css yet; scoped here rather
	   than editing that shared file mid-fan-out (same call admin/+page.svelte
	   already made for --kpi-ok). */
	:global(:root) {
		--preflight-ok: #4ecb8c;
		/* Orange is the "worth saying, not worth stopping for" colour. It is
		   defined next to the green for the same reason that one was scoped
		   here: app.css has no severity tokens yet. */
		--preflight-warn: #e8a33d;
	}
	:global(html[data-theme='light']) {
		--preflight-ok: #2e9e63;
		--preflight-warn: #b3701a;
	}

	.preflight {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		padding: 1rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface);
		color: var(--fg);
	}
	.preflight-brand {
		display: flex;
		align-items: center;
		gap: 0.85rem;
	}
	.preflight-mark {
		width: 3rem;
		height: 3rem;
		flex: none;
		background: url('/favicon.svg') center / contain no-repeat;
	}
	.preflight-name {
		font-family: var(--rb-font-brand);
		display: block;
		font-size: 1.15rem;
		font-weight: 700;
		letter-spacing: -0.01em;
	}
	.preflight-welcome {
		margin: 0.2rem 0 0;
		color: var(--muted);
		font-size: 0.9em;
	}
	.preflight-boot {
		position: fixed;
		inset: 0;
		z-index: var(--preflight-boot-z, 360);
		max-width: 520px;
		margin: 10vh auto;
		height: fit-content;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
		background: var(--surface);
	}
	.preflight-boot::before {
		content: '';
		position: fixed;
		inset: 0;
		z-index: -1;
		background: rgba(0, 0, 0, 0.55);
		pointer-events: none;
	}
	.preflight-boot-strip {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		z-index: 360;
		max-width: none;
		margin: 0;
		border-radius: 0;
		border-top: none;
		border-left: none;
		border-right: none;
		box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
	}
	.preflight-boot-strip h2 {
		font-size: 0.95rem;
	}
	.preflight-boot-strip .preflight-checks {
		flex-direction: row;
		flex-wrap: wrap;
		gap: 0.75rem 1.25rem;
	}
	.preflight-boot-strip :global(.note) {
		display: none;
	}
	.preflight-error {
		color: var(--danger);
	}
	.preflight-checks {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
</style>
