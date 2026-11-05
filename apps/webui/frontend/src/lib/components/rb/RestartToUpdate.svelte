<!--
	RESTART TO UPDATE (INSTALL-40) -- the tray nudge beside the build stamp.

	Renders NOTHING unless the engine reports a pending local build (an agent
	ran `opendj update ready <App.app.tar.gz>`). Then one small button,
	"Restart to update", whose tooltip names the new build's short sha,
	version and build time. Clicking it asks the engine to install, the same
	request `opendj update install` and MCP `app_update_install` make; the
	desktop shell then verifies the signature, replaces the app and relaunches.

	The record is re-read every APP_UPDATE_POLL_MS (1 s) because an agent can flag a
	build, or start an install countdown (INSTALL-43), at any moment. A failed read hides the nudge rather than showing a
	stale one; a refused install is shown in place, verbatim.

	`initial` seeds the first render (the SSR unit test, and any parent that
	already holds the record); the poll takes over after mount.
-->
<script lang="ts">
	import { onMount } from 'svelte';

	import InstallCountdown from './InstallCountdown.svelte';

	import {
		APP_UPDATE_POLL_MS,
		UPDATED_CONFIRM_MS,
		UPDATED_SEEN_KEY,
		fetchAppUpdateStatus,
		descentWarning,
		installPhaseLabel,
		installWasCancelled,
		requestInstall,
		summarizeInstalled,
		summarizePending,
		type AppUpdateStatus,
		type InstallPhase
	} from '$lib/rb/app-update';

	let { initial = null }: { initial?: AppUpdateStatus | null } = $props();

	// svelte-ignore state_referenced_locally
	let status = $state<AppUpdateStatus | null>(initial);
	let fetchedAtMs = $state(Date.now());
	let phase = $state<InstallPhase>('idle');
	let commandId = $state<string | null>(null);
	let refusal = $state<string | null>(null);
	let seenSha = $state<string | null>(readSeenSha());

	const nudge = $derived(summarizePending(status));
	const descent = $derived(descentWarning(status));
	const confirmed = $derived(summarizeInstalled(status, seenSha));

	function readSeenSha(): string | null {
		try {
			return globalThis.localStorage?.getItem(UPDATED_SEEN_KEY) ?? null;
		} catch {
			return null;
		}
	}

	// Show "Updated to <sha>" for a few seconds, then remember it was seen.
	$effect(() => {
		const sha = confirmed === null ? null : (status?.pending?.git_sha_full ?? null);
		if (sha === null) return;
		const timer = setTimeout(() => {
			try {
				globalThis.localStorage?.setItem(UPDATED_SEEN_KEY, sha);
			} catch {
				// Private window or blocked storage: it simply shows again next boot.
			}
			seenSha = sha;
		}, UPDATED_CONFIRM_MS);
		return () => clearTimeout(timer);
	});

	async function refresh(): Promise<void> {
		status = await fetchAppUpdateStatus();
		fetchedAtMs = Date.now();
		// INSTALL-43: the countdown was cancelled (Esc Space x4, CLI or MCP):
		// the click is undone and the nudge is offered again.
		if (phase !== 'idle' && installWasCancelled(status) && status?.install?.command_id === commandId) {
			phase = 'idle';
		}
	}

	async function install(): Promise<void> {
		refusal = null;
		phase = 'installing';
		const outcome = await requestInstall();
		if (outcome.kind === 'refused') {
			phase = 'idle';
			refusal = outcome.reason;
			void refresh();
			return;
		}
		// Accepted: after the INSTALL-43 countdown the shell verifies, swaps the
		// bundle and relaunches this window; "Restarting..." stays until the
		// page goes away, unless the countdown is cancelled.
		commandId = outcome.commandId;
		phase = 'restarting';
		void refresh();
	}

	onMount(() => {
		void refresh();
		const poll = setInterval(() => void refresh(), APP_UPDATE_POLL_MS);
		return () => clearInterval(poll);
	});
</script>

<InstallCountdown {status} {fetchedAtMs} onCancelled={() => void refresh()} />
{#if nudge !== null}
	<button
		type="button"
		class="restart-to-update"
		data-testid="restart-to-update"
		title={refusal === null ? nudge.title : `Install refused: ${refusal}\n\n${nudge.title}`}
		disabled={phase !== 'idle'}
		aria-busy={phase !== 'idle'}
		onclick={() => void install()}
	>
		{#if phase !== 'idle'}<span class="spinner" aria-hidden="true"></span>{/if}
		{installPhaseLabel(phase, nudge.label)}
	</button>
	{#if refusal !== null}
		<span class="refusal" title={refusal}>update refused</span>
	{/if}
	{#if descent !== null}
		<!-- INSTALL-44: an install that would drop the running build's commits. -->
		<span class="descent" class:loud={descent.loud} data-testid="update-descent-warning" title={descent.text}
			>{descent.loud ? descent.text : 'descent unverified'}</span
		>
	{/if}
{:else if confirmed !== null}
	<span class="updated" data-testid="app-updated" title={confirmed.title} role="status">{confirmed.label}</span>
{/if}

<style>
	.restart-to-update {
		padding: 0 0.45rem;
		border: 1px solid currentColor;
		border-radius: 0.6rem;
		background: transparent;
		color: var(--accent, #d97757);
		font: inherit;
		font-weight: 700;
		white-space: nowrap;
		cursor: pointer;
	}
	.restart-to-update:disabled {
		opacity: 0.6;
		cursor: progress;
	}
	.spinner {
		display: inline-block;
		width: 0.6em;
		height: 0.6em;
		margin-right: 0.3em;
		border: 1.5px solid currentColor;
		border-right-color: transparent;
		border-radius: 50%;
		vertical-align: -0.05em;
		animation: update-spin 0.8s linear infinite;
	}
	@keyframes update-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation: none;
		}
	}
	.updated {
		padding: 0 0.45rem;
		border-radius: 0.6rem;
		color: var(--build-ok, #6fbf73);
		font-weight: 700;
		white-space: nowrap;
	}
	.descent {
		margin-left: 0.3rem;
		font-weight: 700;
		color: var(--build-warn, #e0b341);
		white-space: nowrap;
	}
	.descent.loud {
		padding: 0 0.4rem;
		border-radius: 0.3rem;
		background: var(--build-fault, #d93025);
		color: #fff;
		font-weight: 800;
	}
	.refusal {
		margin-left: 0.3rem;
		font-weight: 700;
		color: var(--build-warn, #e0b341);
		white-space: nowrap;
	}
</style>
