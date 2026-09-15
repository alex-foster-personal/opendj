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
	import { onDestroy, onMount } from 'svelte';
	import { OVERLAY_Z } from '$lib/overlays/stack';
	import { bootGateHeading, needsImportAction } from '$lib/preflight/boot-copy';
	import {
		checkPreflight,
		preflightGate,
		requestPermissions
	} from '$lib/preflight/preflight.svelte';
	import {
		IMPORT_MUSIC_LABEL,
		runSetup,
		runSetupBlocked
	} from '$lib/setup/run-setup';
	import PreflightCheckRow from './PreflightCheckRow.svelte';

	const POLL_MS = 3_000;
	const BOOT_WELCOME =
		'Welcome to Open DJ. Import your music to start mixing, or explore with an empty library.';

	let {
		mode = 'boot',
		blocking = true,
		hideCheckIds = [],
		navigate,
		firstRunError = null
	}: {
		mode?: 'boot' | 'admin';
		blocking?: boolean;
		hideCheckIds?: string[];
		navigate?: (path: string) => unknown;
		firstRunError?: string | null;
	} = $props();

	const hiddenIds = $derived(new Set(hideCheckIds));
	const visibleChecks = $derived(
		preflightGate.checks.filter((check) => !hiddenIds.has(check.id))
	);

	let timer: ReturnType<typeof setInterval> | null = null;
	let consecutiveFailPolls = $state(0);
	let importBusy = $state(false);
	let importError = $state<string | null>(null);

	const showImportCta = $derived(
		mode === 'boot' && blocking && needsImportAction(preflightGate.checks)
	);
	const heading = $derived(
		bootGateHeading(blocking, preflightGate.checks, consecutiveFailPolls)
	);

	function stopPolling(): void {
		if (timer === null) return;
		clearInterval(timer);
		timer = null;
	}

	async function pollPreflight(): Promise<void> {
		await checkPreflight();
		if (preflightGate.status === 'fail') {
			consecutiveFailPolls += 1;
		} else if (preflightGate.status === 'pass') {
			consecutiveFailPolls = 0;
		}
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
			consecutiveFailPolls = 0;
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
			firstRunError !== null
	);

	async function handleImportMusic(): Promise<void> {
		if (!navigate || importBusy) return;
		importBusy = true;
		importError = await runSetup(navigate);
		importBusy = false;
	}

	async function retryFirstRun(): Promise<void> {
		await pollPreflight();
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
		<div class="boot-brand">
			<img class="boot-logo" src="/favicon.svg" alt="" width="48" height="48" />
			<p class="boot-welcome">{BOOT_WELCOME}</p>
		</div>
	{/if}
	<h2>{heading}</h2>
	{#if firstRunError}
		<p class="preflight-error" data-testid="preflight-first-run-error">{firstRunError}</p>
		<button type="button" data-testid="preflight-first-run-retry" onclick={() => void retryFirstRun()}>
			Retry
		</button>
	{/if}
	{#if preflightGate.error}
		<p class="preflight-error">Could not reach the engine: {preflightGate.error}</p>
	{/if}
	{#if showImportCta && navigate}
		<button
			type="button"
			class="import-music"
			data-testid="preflight-import-music"
			disabled={importBusy || runSetupBlocked() !== null}
			title={runSetupBlocked() ?? 'Import from a folder or from rekordbox.'}
			onclick={() => void handleImportMusic()}
		>
			{importBusy ? 'Opening setup...' : IMPORT_MUSIC_LABEL}
		</button>
		{#if importError}
			<p class="preflight-error">{importError}</p>
		{/if}
	{/if}
	<ul class="preflight-checks">
		{#each visibleChecks as check (check.id)}
			<PreflightCheckRow {check} {navigate} {mode} />
		{/each}
	</ul>
	<div class="preflight-actions">
		<button type="button" onclick={() => void checkPreflight()}>Re-check</button>
		<button
			type="button"
			title="Attempt the gated audio read again; macOS prompts here if it never has."
			onclick={() => void requestPermissions()}
		>
			Re-request permissions
		</button>
	</div>
	{#if mode === 'boot' && blocking}
		<p class="note">This screen clears itself automatically once every check passes.</p>
	{/if}
</section>
{/if}

<style>
	/* No success/positive token exists in app.css yet; scoped here rather
	   than editing that shared file mid-fan-out (same call admin/+page.svelte
	   already made for --kpi-ok). */
	:global(:root) {
		--preflight-ok: #4ecb8c;
	}
	:global(html[data-theme='light']) {
		--preflight-ok: #2e9e63;
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
	.boot-brand {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
		text-align: center;
	}
	.boot-logo {
		border-radius: 12px;
	}
	.boot-welcome {
		margin: 0;
		color: var(--muted);
		font-size: 0.95em;
		line-height: 1.4;
	}
	.import-music {
		align-self: stretch;
		padding: 0.65rem 1rem;
		font-size: 1rem;
		font-weight: 600;
		background: var(--accent);
		color: var(--bg);
		border: none;
		border-radius: 6px;
		cursor: pointer;
	}
	.import-music:disabled {
		opacity: 0.55;
		cursor: not-allowed;
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
	.preflight-boot-strip .note {
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
	.preflight-actions {
		display: flex;
		gap: 0.5rem;
	}
	.note {
		color: var(--muted);
		font-size: 0.85em;
		margin: 0;
	}
</style>
