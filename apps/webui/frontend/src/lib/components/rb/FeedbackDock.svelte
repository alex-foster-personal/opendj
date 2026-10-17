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
		<FeedbackPinShellButton overlay />
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
		/* Clear of the window edge and of the bottom tray, where the version
		   number and age sit (about 18-20px). */
		right: 20px;
		bottom: 36px;
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
		position: relative;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 32px;
		padding: 0 0 1px;
		border-radius: 14px;
		border: 2px solid #ffb43a;
		/* A fixed dark face, not var(--surface): the yellow glyph on the light
		   theme's surface reads 1.74:1, below WCAG AA. Dark keeps it near 10:1
		   and the bubble identical on every page. */
		background: #121720;
		color: #ffb43a;
		font-size: 16px;
		font-weight: 700;
		line-height: 1;
		cursor: pointer;
		box-shadow: 0 2px 6px rgb(0 0 0 / 35%);
	}
	.fb-support-bubble::after {
		content: '';
		position: absolute;
		left: 8px;
		bottom: -6px;
		width: 8px;
		height: 8px;
		background: #121720;
		border-right: 2px solid #ffb43a;
		border-bottom: 2px solid #ffb43a;
		transform: rotate(40deg);
	}
	.fb-support-bubble:hover {
		color: #ffb43a;
		border-color: #ffb43a;
	}
</style>
