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
	 */
	import { onDestroy, onMount } from 'svelte';
	import type { PreflightCheck } from '$lib/api';
	import {
		checkPreflight,
		preflightGate,
		requestPermissions
	} from '$lib/preflight/preflight.svelte';

	const POLL_MS = 3_000;

	let { mode = 'boot' }: { mode?: 'boot' | 'admin' } = $props();

	let timer: ReturnType<typeof setInterval> | null = null;

	function stopPolling(): void {
		if (timer === null) return;
		clearInterval(timer);
		timer = null;
	}

	function ensurePolling(): void {
		if (timer !== null) return;
		timer = setInterval(() => void checkPreflight(), POLL_MS);
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
		void checkPreflight();
	});

	onDestroy(stopPolling);

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
</script>

<section
	class="preflight"
	class:preflight-boot={mode === 'boot'}
	class:preflight-admin={mode === 'admin'}
	data-preflight-mode={mode}
	data-preflight-status={preflightGate.status}
>
	<h2>{mode === 'boot' ? 'Starting up' : 'Preflight'}</h2>
	{#if preflightGate.error}
		<p class="preflight-error">Could not reach the engine: {preflightGate.error}</p>
	{/if}
	<ul class="preflight-checks">
		{#each preflightGate.checks as check (check.id)}
			<li data-check-id={check.id} data-check-status={check.status}>
				<details open={check.status !== 'pass'}>
					<summary>
						<span
							class="light {lightClass(check.status)}"
							title={`${check.label}: ${check.status}`}
						></span>
						<span class="label">{check.label}</span>
						<span class="status-word">{check.status}</span>
					</summary>
					<p class="detail" title={check.detail}>{check.detail}</p>
					{#if check.remediation}
						<p class="remediation">{check.remediation}</p>
						{#if settingsUrl(check.remediation)}
							<a href={settingsUrl(check.remediation)} class="settings-link">
								Open System Settings
							</a>
						{/if}
					{/if}
				</details>
			</li>
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
	{#if mode === 'boot'}
		<p class="note">This screen clears itself automatically once every check passes.</p>
	{/if}
</section>

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
		z-index: 1000;
		max-width: 520px;
		margin: 10vh auto;
		height: fit-content;
		box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
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
	.preflight-checks summary {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		cursor: pointer;
	}
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
