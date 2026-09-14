<script lang="ts">
	/**
	 * Full-screen blocker for Google sign-in. Shown for the whole attempt:
	 * briefly on the in-page redirect path, and for the full desktop-shell
	 * system-browser poll.
	 */
	import {
		cancelSignIn,
		signInOverlay,
		signInStatusText
	} from '$lib/auth/sign-in-overlay.svelte';

	function onBackdropKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		if (signInOverlay.phase !== 'waiting') return;
		event.preventDefault();
		event.stopPropagation();
		cancelSignIn();
	}
</script>

{#if signInOverlay.open}
	<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
	<div
		class="si-backdrop"
		role="alertdialog"
		aria-modal="true"
		aria-labelledby="si-status"
		tabindex="-1"
		onkeydown={onBackdropKeydown}
	>
		<div class="si-panel">
			<div class="si-spinner" aria-hidden="true"></div>
			<p id="si-status" class="si-status">{signInStatusText(signInOverlay.phase)}</p>
			{#if signInOverlay.phase === 'waiting'}
				<button
					type="button"
					class="si-cancel"
					onclick={() => cancelSignIn()}
					title="Stop waiting for Google sign-in in your browser."
				>
					Cancel
				</button>
			{/if}
		</div>
	</div>
{/if}

<style>
	.si-backdrop {
		position: fixed;
		inset: 0;
		z-index: 390;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 1rem;
		background: rgb(0 0 0 / 72%);
	}
	.si-panel {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 1rem;
		max-width: 22rem;
		padding: 1.5rem 1.75rem;
		border: 1px solid var(--border, #1c222c);
		border-radius: 12px;
		background: var(--surface, #121720);
		box-shadow: 0 18px 50px rgb(0 0 0 / 50%);
		color: var(--fg);
		text-align: center;
	}
	.si-spinner {
		width: 2rem;
		height: 2rem;
		border: 2px solid var(--border, #1c222c);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: si-spin 0.8s linear infinite;
	}
	@keyframes si-spin {
		to {
			transform: rotate(360deg);
		}
	}
	.si-status {
		margin: 0;
		font-size: 0.9rem;
		line-height: 1.45;
	}
	.si-cancel {
		font: inherit;
		font-size: 0.82rem;
		padding: 0.35rem 0.9rem;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--fg);
		cursor: pointer;
	}
	.si-cancel:hover {
		border-color: var(--accent);
		color: var(--accent);
	}
</style>
