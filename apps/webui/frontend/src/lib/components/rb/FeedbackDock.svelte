<script lang="ts">
	/** Always-on comment pin + support entry (FB-18c, #3981). */
	import { OVERLAY_Z_INDEX } from '$lib/overlays/overlay-stack';
	import FeedbackPinShellButton from './FeedbackPinShellButton.svelte';
	import FeedbackSupportPanel from './FeedbackSupportPanel.svelte';

	let supportOpen = $state(false);
</script>

<div class="fb-dock" style:z-index={OVERLAY_Z_INDEX.feedbackDock}>
	{#if supportOpen}
		<div class="fb-dock-panel">
			<FeedbackSupportPanel onclose={() => (supportOpen = false)} />
		</div>
	{/if}
	<div class="fb-dock-actions">
		<FeedbackPinShellButton />
		<button
			type="button"
			class="fb-support-bubble"
			aria-label="Open support and feedback"
			aria-expanded={supportOpen}
			title="Support and general feedback"
			onclick={() => (supportOpen = !supportOpen)}
		>
			?
		</button>
	</div>
</div>

<style>
	.fb-dock {
		position: fixed;
		right: 12px;
		bottom: 12px;
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 8px;
		pointer-events: none;
	}
	.fb-dock-panel,
	.fb-dock-actions {
		pointer-events: auto;
	}
	.fb-dock-actions {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 8px;
	}
	.fb-support-bubble {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		padding: 0;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--muted);
		font-size: 16px;
		font-weight: 700;
		line-height: 1;
		cursor: pointer;
	}
	.fb-support-bubble:hover {
		color: var(--accent);
		border-color: var(--accent);
	}
</style>
