<script lang="ts">
	/**
	 * One row of PREFLIGHT-01/02's boot gate (issue #771, issue #2589).
	 *
	 * Split out of PreflightScreen.svelte as a pure presentational leaf with
	 * NO lifecycle hooks of its own (no onMount/onDestroy/$effect): that
	 * parent's polling loop registers onDestroy, which only makes sense for a
	 * component that gets mounted-then-later-destroyed in a browser, and
	 * throws under `svelte/server`'s one-shot `render()` (there is nothing to
	 * destroy after a single server render). This row has nothing like that,
	 * so it CAN be mounted through the real Svelte SSR renderer -- which is
	 * how issue #2589's dismissed-empty "Run setup" control is proven to
	 * actually render, rather than merely grepped for in source.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ 🎯 a `library-attached` row that is `pending` because setup was
	 *     dismissed with an empty library shows a real, working "Run setup"
	 *     control -- never a dead end with only prose remediation.
	 *     [if] this state renders no control, or a disabled/inert one, while
	 *     the daemon is reachable [then ⛔️] broken
	 *   ✔︎ 🎯 every other row (including every OTHER `pending` row, e.g.
	 *     audio-access with nothing sampleable) renders no such control --
	 *     "Run setup" only ever appears where it would do something.
	 *     [if] any check id other than library-attached offers Run setup
	 *     [then ⛔️] broken
	 */
	import type { PreflightCheck } from '$lib/api';
	import { checkOffersRunSetup } from '$lib/preflight/preflight-actions';
	import { RUN_SETUP_LABEL, runSetup, runSetupBlocked } from '$lib/setup/run-setup';

	let {
		check,
		navigate,
		mode = 'boot',
		label: labelOverride,
		detail: detailOverride
	}: {
		check: PreflightCheck;
		navigate?: ((path: string) => unknown) | undefined;
		mode?: 'boot' | 'admin';
		label?: string;
		detail?: string;
	} = $props();

	let setupBusy = $state(false);
	let setupError = $state<string | null>(null);

	const offersRunSetup = $derived(checkOffersRunSetup(check));

	const displayLabel = $derived(
		labelOverride ??
			(mode === 'boot' && check.user_label ? check.user_label : check.label)
	);
	const displayDetail = $derived(
		detailOverride ??
			(mode === 'boot' && check.user_detail ? check.user_detail : check.detail)
	);
	const displayRemediation = $derived(
		mode === 'boot' && check.user_remediation ? check.user_remediation : check.remediation
	);

	/**
	 * Three colours, and the third one carries information.
	 *
	 * Green: this passed. Red: this failed AND the app cannot usefully run
	 * until it is fixed. Orange: this needs saying but the app runs anyway,
	 * which covers both a failed advisory check and one that could not be
	 * exercised at all.
	 *
	 * Painting every non-pass red (what shipped before) taught the user that
	 * red does not mean stop, which costs the colour its meaning on the one
	 * row where it does.
	 */
	function lightClass(check: PreflightCheck): string {
		if (check.status === 'pass') return 'light-pass';
		if (check.status === 'fail' && severityOf(check) === 'blocking') return 'light-fail';
		return 'light-warn';
	}

	/** Unknown severity reads as blocking, matching the server's default. */
	function severityOf(check: PreflightCheck): string {
		return check.severity ?? 'blocking';
	}

	/** Plain-language "what is this, and what do I do" for the hover. */
	function explainerOf(check: PreflightCheck): string | null {
		return check.explainer ?? null;
	}

	const lightTitle = $derived.by(() => {
		const meaning =
			check.status === 'pass'
				? 'Working.'
				: check.status === 'fail' && severityOf(check) === 'blocking'
					? 'Not working, and Open DJ needs this before it can run.'
					: check.status === 'fail'
						? 'Not working, but you can carry on; fixing it is optional.'
						: 'Not checked yet; you can carry on.';
		const explainer = explainerOf(check);
		return explainer ? `${displayLabel}: ${meaning} ${explainer}` : `${displayLabel}: ${meaning}`;
	});

	/** The `x-apple.systempreferences:` deep link inside a remediation
	 * sentence, or null when the remediation carries no such link (e.g. the
	 * state-db check's remediation, which only ever names a restart). */
	function settingsUrl(remediation: string | null | undefined): string | null {
		if (!remediation) return null;
		const match = remediation.match(/x-apple\.systempreferences:\S+/);
		return match ? match[0] : null;
	}

	async function handleRunSetup(): Promise<void> {
		if (!navigate || setupBusy) return;
		setupBusy = true;
		setupError = await runSetup(navigate);
		setupBusy = false;
	}
</script>

<li data-check-id={check.id} data-check-status={check.status}>
	<details open={check.status !== 'pass'}>
		<summary>
			<span class="light {lightClass(check)}" title={lightTitle}></span>
			<span class="label" title={lightTitle}>{displayLabel}</span>
			{#if mode === 'admin'}
				<span class="status-word">{check.status}</span>
			{/if}
		</summary>
		<p class="detail" title={explainerOf(check) ?? displayDetail}>{displayDetail}</p>
		{#if explainerOf(check)}
			<p class="explainer">{explainerOf(check)}</p>
		{/if}
		{#if displayRemediation}
			<p class="remediation">{displayRemediation}</p>
			{#if settingsUrl(displayRemediation)}
				<a href={settingsUrl(displayRemediation)} class="settings-link"> Open System Settings </a>
			{/if}
		{/if}
		{#if offersRunSetup}
			<button
				type="button"
				class="run-setup"
				data-testid="preflight-run-setup"
				disabled={setupBusy || runSetupBlocked() !== null}
				onclick={() => void handleRunSetup()}
			>
				{setupBusy ? 'Opening setup...' : RUN_SETUP_LABEL}
			</button>
			{#if setupError}
				<p class="preflight-error">{setupError}</p>
			{/if}
		{/if}
	</details>
</li>

<style>
	.light {
		display: inline-block;
		width: 10px;
		height: 10px;
		border-radius: 50%;
		flex: none;
	}
	.light-pass {
		background: var(--preflight-ok);
	}
	.light-fail {
		background: var(--danger);
	}
	.light-warn {
		background: var(--preflight-warn);
	}
	.label {
		font-weight: 600;
	}
	.status-word {
		color: var(--muted);
		font-size: 0.85em;
	}
	.detail,
	.explainer,
	.remediation {
		margin: 0.25rem 0 0 1.5rem;
		color: var(--muted);
		font-size: 0.9em;
	}
	.settings-link {
		margin-left: 1.5rem;
		display: inline-block;
	}
	.run-setup {
		margin: 0.4rem 0 0 1.5rem;
	}
	.preflight-error {
		color: var(--danger);
	}
	li {
		list-style: none;
	}
	details summary {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		cursor: pointer;
	}
</style>
