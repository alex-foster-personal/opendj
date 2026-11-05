<!--
	INSTALL COUNTDOWN (INSTALL-43) -- the overlay before a local build installs.

	the maintainer, Tue 3 Nov 2026: an agent's build installs immediately, even mid-play,
	and the ONLY cancel is Esc Space Esc Space Esc Space Esc Space. Every
	`update install` (the tray click, `opendj update install`, MCP
	`app_update_install`) is held back by the engine for 10 s; this overlay
	reads that countdown from the status the tray already polls and shows
	"Updating to <sha> in N s. Press Esc Space ×4 to cancel."

	The combo is read in the webview: the app has focus while it plays, so no
	Input Monitoring grant is involved. While the countdown runs, Esc and Space
	are swallowed (capture phase, stopImmediatePropagation) so typing the combo
	never also pauses a deck or closes a panel; every other key passes through
	and restarts the combo. Completing it posts the same cancel request as
	`opendj update cancel`.

	Renders nothing when no install is counting down.
-->
<script lang="ts">
	import { onMount } from 'svelte';

	import {
		CANCEL_COMBO,
		advanceCancelCombo,
		comboComplete,
		isComboKey,
		requestCancel,
		summarizeCountdown,
		type AppUpdateStatus
	} from '$lib/rb/app-update';

	let {
		status,
		fetchedAtMs,
		onCancelled
	}: { status: AppUpdateStatus | null; fetchedAtMs: number; onCancelled: () => void } = $props();

	let nowMs = $state(Date.now());
	let progress = $state(0);
	let refusal = $state<string | null>(null);
	let cancelling = $state(false);

	const countdown = $derived(summarizeCountdown(status, Math.max(0, nowMs - fetchedAtMs)));

	async function cancel(): Promise<void> {
		cancelling = true;
		const outcome = await requestCancel('Esc Space x4 in the app');
		cancelling = false;
		progress = 0;
		refusal = outcome.kind === 'refused' ? outcome.reason : null;
		onCancelled();
	}

	function onKeydown(event: KeyboardEvent): void {
		if (countdown === null || cancelling) return;
		if (isComboKey(event.key)) {
			event.preventDefault();
			event.stopImmediatePropagation();
		}
		progress = advanceCancelCombo(progress, event.key, event.repeat);
		if (comboComplete(progress)) void cancel();
	}

	$effect(() => {
		if (countdown === null) progress = 0;
	});

	onMount(() => {
		const tick = setInterval(() => (nowMs = Date.now()), 200);
		window.addEventListener('keydown', onKeydown, { capture: true });
		return () => {
			clearInterval(tick);
			window.removeEventListener('keydown', onKeydown, { capture: true });
		};
	});
</script>

{#if countdown !== null}
	<div class="install-countdown" data-testid="install-countdown" role="alertdialog" aria-live="assertive">
		<p class="text">{countdown.text}</p>
		{#if countdown.warning !== null}
			<p class="descent" data-testid="install-descent-warning">{countdown.warning}</p>
		{/if}
		<p class="combo" aria-label="cancel combo progress">
			{#each CANCEL_COMBO as key, index (index)}
				<span class="key" class:hit={index < progress}>{key === ' ' ? 'Space' : 'Esc'}</span>
			{/each}
		</p>
		{#if refusal !== null}<p class="refusal">Cancel refused: {refusal}</p>{/if}
	</div>
{/if}

<style>
	.install-countdown {
		position: fixed;
		left: 50%;
		bottom: 3rem;
		transform: translateX(-50%);
		z-index: 1000;
		max-width: min(36rem, calc(100vw - 2rem));
		padding: 0.8rem 1.1rem;
		border: 2px solid var(--build-warn, #e0b341);
		border-radius: 0.6rem;
		background: var(--surface-overlay, rgba(16, 16, 16, 0.94));
		color: var(--text-strong, #fff);
		box-shadow: 0 0.4rem 1.6rem rgba(0, 0, 0, 0.5);
		text-align: center;
	}
	.text {
		margin: 0;
		font-size: 1.05rem;
		font-weight: 700;
	}
	.descent {
		margin: 0.4rem 0 0;
		font-weight: 800;
		color: var(--build-fault, #ff4d4d);
	}
	.combo {
		margin: 0.5rem 0 0;
		display: flex;
		gap: 0.3rem;
		justify-content: center;
	}
	.key {
		padding: 0 0.35rem;
		border: 1px solid currentColor;
		border-radius: 0.3rem;
		font-size: 0.75rem;
		opacity: 0.55;
	}
	.key.hit {
		opacity: 1;
		background: var(--build-warn, #e0b341);
		color: #111;
	}
	.refusal {
		margin: 0.4rem 0 0;
		color: var(--build-warn, #e0b341);
	}
</style>
