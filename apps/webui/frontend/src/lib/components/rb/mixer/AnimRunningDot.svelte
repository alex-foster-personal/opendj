<script lang="ts">
	/**
	 * ANIM-02: the small running dot on a dial or fader while a ramp animates it
	 * (the Animate box, `animate_control`, or an agent `ramp` order). Its
	 * parent must be positioned; the dot sits in its top-right corner.
	 */
	import { dialAnimations } from '$lib/rb/dial-animations.svelte';

	let { animKey }: { animKey: string } = $props();
	const running = $derived(dialAnimations.running[animKey]);
</script>

{#if running !== undefined}
	<span
		class="anim-dot"
		role="status"
		title={`animating - ${running.label}. Move the control to take over.`}
		aria-label={`animating: ${running.label}`}
		data-testid={`anim-running-${animKey}`}
	></span>
{/if}

<style>
	.anim-dot {
		position: absolute;
		top: 0;
		right: 0;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--rb-accent, #f80);
		box-shadow: 0 0 4px var(--rb-accent, #f80);
		pointer-events: auto;
		animation: anim-dot-pulse 0.9s ease-in-out infinite alternate;
	}
	@keyframes anim-dot-pulse {
		from {
			opacity: 1;
		}
		to {
			opacity: 0.35;
		}
	}
</style>
