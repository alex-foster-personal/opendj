<script lang="ts">
	import { OVERLAY_Z_INDEX } from '$lib/overlays/overlay-stack';
	import { cancelQuit, confirmQuit } from '$lib/shell/quit-gate';
	import { isQuitConfirmOpen, subscribeQuitConfirmOpen } from '$lib/shell/quit-gate-state';

	let open = $state(false);

	$effect(() => {
		const sync = (): void => {
			open = isQuitConfirmOpen();
		};
		sync();
		return subscribeQuitConfirmOpen(sync);
	});
</script>

{#if open}
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="qc-backdrop"
		role="presentation"
		style:z-index={OVERLAY_Z_INDEX.quitConfirm}
	>
		<div
			class="qc-panel"
			role="dialog"
			aria-modal="true"
			aria-label="QUIT: are you sure?"
			data-testid="quit-confirm-dialog"
			tabindex="-1"
		>
			<h2 class="qc-title">QUIT: are you sure?</h2>
			<p class="qc-helper">cmd q again or enter to confirm</p>
			<div class="qc-actions">
				<button type="button" class="qc-btn qc-cancel" onclick={() => cancelQuit()}>
					Cancel
				</button>
				<button type="button" class="qc-btn qc-confirm" onclick={() => void confirmQuit()}>
					Confirm
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.qc-backdrop {
		position: fixed;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgb(0 0 0 / 0.55);
	}
	.qc-panel {
		min-width: 18rem;
		max-width: 24rem;
		padding: 1.25rem 1.5rem;
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		background: var(--surface);
		color: var(--text);
		box-shadow: 0 0.5rem 2rem rgb(0 0 0 / 0.35);
	}
	.qc-title {
		margin: 0;
		font-size: 1.05rem;
		font-weight: 600;
	}
	.qc-helper {
		margin: 0.5rem 0 1rem;
		font-size: 0.8rem;
		color: var(--muted, #888);
	}
	.qc-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
	}
	.qc-btn {
		padding: 0.35rem 0.85rem;
		border: 1px solid var(--border);
		border-radius: 0.25rem;
		background: var(--surface-2, var(--surface));
		color: inherit;
		font: inherit;
		cursor: pointer;
	}
	.qc-confirm {
		border-color: var(--accent);
		background: var(--accent);
		color: var(--accent-contrast, #fff);
	}
</style>
