<script lang="ts">
	/**
	 * Live TopBar pill for the dual-deck blend (TRANS-01, issue #324).
	 * Status only: not a button, no tab focus on the label, pointer-events
	 * none on the label. The pill itself takes hover so it can explain what
	 * it measures (TRANS-02), and swallows mousedown so a press on it cannot
	 * steal deck focus. Hidden while idle so the chip takes no TopBar width.
	 */
	import { transitionChipTitle } from '$lib/rb/transition-chip-explainer';
	import { readTransition } from '$lib/rb/transition-read.svelte';
	import { getTransitionStatusLight } from '$lib/rb/transition-status-light';

	const status = $derived(readTransition());
	const lightTitle = $derived(getTransitionStatusLight().describe());
	const chipTitle = $derived(transitionChipTitle(status));

	function keepDeckFocus(event: MouseEvent): void {
		event.preventDefault();
	}
</script>

{#if status.state !== 'idle'}
	<div
		class="transition-chip"
		data-transition={status.state}
		data-testid="transition-chip"
		aria-label={status.state}
		title={chipTitle}
		role="presentation"
		onmousedown={keepDeckFocus}
	>
		<span class="transition-light" title={lightTitle}></span>
		<span class="transition-label" role="status" aria-live="polite">
			{#if status.state === 'transitioning'}
				<span class="transition-dot">•</span> transitioning
			{:else}
				approaching
			{/if}
		</span>
	</div>
{/if}

<style>
	.transition-chip {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		flex: none;
		max-width: 110px;
		padding: 1px 7px;
		border-radius: 999px;
		border: 1px solid color-mix(in srgb, var(--rb-text-dim, #838990) 45%, transparent);
		white-space: nowrap;
		pointer-events: auto;
		cursor: help;
		line-height: 1.2;
		font-size: 10px;
		letter-spacing: 0.02em;
		color: var(--rb-text, #c8cdd2);
		background: color-mix(in srgb, var(--rb-text, #c8cdd2) 8%, transparent);
	}
	.transition-chip[data-transition='approaching'] {
		color: var(--rb-text-dim, #838990);
	}
	.transition-label {
		pointer-events: none;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.transition-chip[data-transition='approaching'] .transition-label {
		opacity: 0.65;
		animation: transition-approach-pulse 1.2s ease-in-out infinite;
	}
	.transition-dot {
		color: #d0342c;
	}
	.transition-light {
		width: 8px;
		height: 8px;
		flex: none;
		border-radius: 50%;
		background: var(--rb-text-dim, #838990);
		opacity: 0.35;
		pointer-events: auto;
	}
	@keyframes transition-approach-pulse {
		50% {
			opacity: 0.35;
		}
	}
</style>
