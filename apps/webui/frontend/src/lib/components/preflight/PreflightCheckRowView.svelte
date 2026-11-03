<script lang="ts">
	/**
	 * Props-only half of PreflightCheckRow: one row of the boot gate. The
	 * container owns Run setup (it reaches the engine); this file draws the
	 * light, label, detail and remediation, and the Run setup control when
	 * the container says the row offers it. No lifecycle hooks, so it still
	 * renders under svelte/server like the container it replaced.
	 *
	 * `check` is typed as the slice of PreflightCheck this row reads, so the
	 * view adds no importer to api.ts.
	 */
	type PreflightCheck = {
		id: string;
		status: 'pass' | 'fail' | 'pending';
		label: string;
		detail: string;
		severity?: string | null | undefined;
		explainer?: string | null | undefined;
		remediation?: string | null | undefined;
		user_label?: string | null | undefined;
		user_detail?: string | null | undefined;
		user_remediation?: string | null | undefined;
	};

	let {
		check,
		mode = 'boot',
		label: labelOverride,
		detail: detailOverride,
		offersRunSetup,
		runSetupLabel,
		setupBusy,
		setupError,
		isRunSetupBlocked,
		onRunSetup
	}: {
		check: PreflightCheck;
		mode?: 'boot' | 'admin';
		label?: string | undefined;
		detail?: string | undefined;
		offersRunSetup: boolean;
		runSetupLabel: string;
		setupBusy: boolean;
		setupError: string | null;
		/** Read at render, as the row always did, so a refusal shows at once. */
		isRunSetupBlocked: () => boolean;
		onRunSetup: () => void;
	} = $props();

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
				disabled={setupBusy || isRunSetupBlocked()}
				onclick={() => onRunSetup()}
			>
				{setupBusy ? 'Opening setup...' : runSetupLabel}
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
