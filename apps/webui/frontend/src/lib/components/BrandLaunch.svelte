<script lang="ts">
	import { onMount } from 'svelte';
	import {
		BRAND_LAUNCH_DURATION_MS,
		BRAND_LAUNCH_FADE_MS,
		BRAND_LAUNCH_HOLD_MS,
		BRAND_LAUNCH_SLIDE_MS,
		completeBrandLaunch,
		shouldPlayBrandLaunch
	} from '$lib/brand-launch';

	/**
	 * Whether to play now, and what to do when the animation ends. The defaults
	 * read and write the per-browser marker; a story passes its own answers, so
	 * its state never depends on what an earlier render left in localStorage.
	 */
	/**
	 * Static capture state: the overlay shows and holds the meet frame (both
	 * halves in place, no slide, no fade), so a screenshot cannot catch it
	 * blank after a finite animation has run out. Never set in the app.
	 */
	let {
		shouldPlay = () => shouldPlayBrandLaunch(window.localStorage),
		onfinish = () => completeBrandLaunch(window.localStorage),
		pinned = false
	}: { shouldPlay?: () => boolean; onfinish?: () => void; pinned?: boolean } = $props();

	let visible = $state(false);

	onMount(() => {
		visible = shouldPlay();
	});

	function finish(event: AnimationEvent): void {
		if (event.target !== event.currentTarget) return;
		onfinish();
		visible = false;
	}
</script>

{#if visible}
	<div
		class="brand-launch"
		class:brand-launch--pinned={pinned}
		role="img"
		aria-label="Open DJ launch animation"
		style="
			--brand-slide-ms: {BRAND_LAUNCH_SLIDE_MS}ms;
			--brand-hold-ms: {BRAND_LAUNCH_HOLD_MS}ms;
			--brand-fade-ms: {BRAND_LAUNCH_FADE_MS}ms;
			--brand-total-ms: {BRAND_LAUNCH_DURATION_MS}ms;
		"
		onanimationend={finish}
	>
		<div class="brand-mark" aria-hidden="true">
			<svg class="brand-mark-svg" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
				<path
					class="brand-half-dark"
					data-testid="brand-launch-half-dark"
					d="M 367.72,144.28 A 188.00,188.00 0 0,0 101.85,410.15 Z"
					fill="#9c4b34"
				/>
				<path
					class="brand-half-light"
					data-testid="brand-launch-half-light"
					d="M 410.15,101.85 A 188.00,188.00 0 0,1 144.28,367.72 Z"
					fill="#D97757"
				/>
			</svg>
		</div>
	</div>
{/if}

<style>
	.brand-launch {
		position: fixed;
		inset: 0;
		z-index: 1200;
		display: grid;
		place-items: center;
		background: #050505;
		pointer-events: none;
		animation: launch-fade var(--brand-fade-ms) ease-out forwards;
		animation-delay: calc(var(--brand-slide-ms) + var(--brand-hold-ms));
	}
	.brand-mark {
		width: clamp(7rem, 20vw, 12rem);
		aspect-ratio: 1;
	}
	.brand-mark-svg {
		display: block;
		width: 100%;
		height: 100%;
	}
	.brand-half-dark,
	.brand-half-light {
		animation-duration: var(--brand-slide-ms);
		animation-timing-function: ease-out;
		animation-fill-mode: both;
	}
	.brand-half-dark {
		animation-name: brand-half-slide-dark;
	}
	.brand-half-light {
		animation-name: brand-half-slide-light;
	}
	@keyframes brand-half-slide-dark {
		from {
			transform: translate(-28%, 28%);
		}
		to {
			transform: translate(0, 0);
		}
	}
	@keyframes brand-half-slide-light {
		from {
			transform: translate(28%, -28%);
		}
		to {
			transform: translate(0, 0);
		}
	}
	@keyframes launch-fade {
		from {
			opacity: 1;
		}
		to {
			opacity: 0;
		}
	}
	.brand-launch--pinned,
	.brand-launch--pinned .brand-half-dark,
	.brand-launch--pinned .brand-half-light {
		animation: none;
	}
	@media (prefers-reduced-motion: reduce) {
		.brand-half-dark,
		.brand-half-light {
			animation: none;
			transform: none;
		}
	}
</style>
