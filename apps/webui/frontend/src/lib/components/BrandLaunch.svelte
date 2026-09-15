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

	let visible = $state(false);

	onMount(() => {
		visible = shouldPlayBrandLaunch(window.localStorage);
	});

	function finish(event: AnimationEvent): void {
		if (event.target !== event.currentTarget) return;
		completeBrandLaunch(window.localStorage);
		visible = false;
	}
</script>

{#if visible}
	<div
		class="brand-launch"
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
	@media (prefers-reduced-motion: reduce) {
		.brand-half-dark,
		.brand-half-light {
			animation: none;
			transform: none;
		}
	}
</style>
