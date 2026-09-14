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
	import { RUN_SETUP_LABEL, runSetup, runSetupBlocked } from '$lib/setup/run-setup';

	let {
		check,
		navigate
	}: { check: PreflightCheck; navigate?: ((path: string) => unknown) | undefined } = $props();

	let setupBusy = $state(false);
	let setupError = $state<string | null>(null);

	const offersRunSetup = $derived(check.id === 'library-attached' && check.status === 'pending');

	function lightClass(status: PreflightCheck['status']): string {
		if (status === 'pass') return 'light-pass';
		if (status === 'fail') return 'light-fail';
		return 'light-pending';
	}

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
			<span class="light {lightClass(check.status)}" title={`${check.label}: ${check.status}`}
			></span>
			<span class="label">{check.label}</span>
			<span class="status-word">{check.status}</span>
		</summary>
		<p class="detail" title={check.detail}>{check.detail}</p>
		{#if check.remediation}
			<p class="remediation">{check.remediation}</p>
			{#if settingsUrl(check.remediation)}
				<a href={settingsUrl(check.remediation)} class="settings-link"> Open System Settings </a>
			{/if}
		{/if}
		{#if offersRunSetup}
			<button
				type="button"
				class="run-setup"
				data-testid="preflight-run-setup"
				disabled={setupBusy || runSetupBlocked() !== null}
				title={runSetupBlocked() ?? 'Import a library now, or keep using the app empty.'}
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
	.light-pending {
		background: var(--accent);
	}
	.label {
		font-weight: 600;
	}
	.status-word {
		color: var(--muted);
		font-size: 0.85em;
	}
	.detail,
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
