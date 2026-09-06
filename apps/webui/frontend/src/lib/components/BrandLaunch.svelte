<script lang="ts">
	import { onMount } from 'svelte';
	import { completeBrandLaunch, shouldPlayBrandLaunch } from '$lib/brand-launch';
	import OdjWordmark from './OdjWordmark.svelte';

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
	<div class="brand-launch" aria-label="Open DJ launch animation" onanimationend={finish}>
		<div class="brand-orbit" aria-hidden="true"><span></span><span></span></div>
		<div class="brand-lockup"><OdjWordmark /><span>open Dj</span></div>
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
		animation: launch-fade 2.7s ease-out forwards;
	}
	.brand-orbit {
		position: relative;
		width: clamp(7rem, 20vw, 12rem);
		aspect-ratio: 1;
		animation: orbit-turn 2.2s cubic-bezier(0.2, 0.75, 0.25, 1) forwards;
	}
	.brand-orbit span {
		position: absolute;
		inset: 0;
		border: clamp(0.45rem, 1.25vw, 0.8rem) solid;
		border-radius: 50%;
	}
	.brand-orbit span:first-child {
		border-color: #fff transparent transparent #fff;
		transform: translate(-2%, -2%);
	}
	.brand-orbit span:last-child {
		border-color: transparent #d97757 #d97757 transparent;
		transform: translate(2%, 2%);
	}
	.brand-lockup {
		position: absolute;
		display: grid;
		justify-items: center;
		color: #050505;
		font-size: clamp(0.76rem, 1.8vw, 1rem);
		animation: mark-arrive 2.7s ease-out forwards;
	}
	.brand-lockup :global(.odj-mark) { color: #fff; font-size: clamp(2rem, 5vw, 3.4rem); }
	.brand-lockup > span { margin-top: 0.2rem; color: #fff; font-weight: 600; }
	@keyframes orbit-turn {
		0% { transform: rotate(0deg) scale(0.78); }
		35% { transform: rotate(540deg) scale(1.16); }
		70% { transform: rotate(820deg) scale(1.04); }
		100% { transform: rotate(900deg) scale(1); }
	}
	@keyframes mark-arrive {
		0%, 62% { opacity: 0; transform: translateY(0.5rem) scale(0.92); }
		100% { opacity: 1; transform: translateY(0) scale(1); }
	}
	@keyframes launch-fade {
		0%, 83% { opacity: 1; }
		100% { opacity: 0; }
	}
	@media (prefers-reduced-motion: reduce) {
		.brand-launch, .brand-orbit, .brand-lockup { animation-duration: 1ms; }
	}
</style>
