<script lang="ts">
	import { onMount } from 'svelte';
	import { completeBrandLaunch, shouldPlayBrandLaunch } from '$lib/brand-launch';

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
		<!-- The shipped mark itself, not an imitation of it. Spinning the real
		     artwork is also what the maintainer's OPS-10 note asked for: the two shades
		     meet along a diagonal, so the split IS the divider line that lets
		     you see it spin, and it decelerates onto the finished logo. -->
		<div class="brand-mark" aria-hidden="true"></div>
		<div class="brand-lockup"><span class="brand-name">Open DJ</span></div>
	</div>
{/if}

<style>
	.brand-launch {
		position: fixed;
		inset: 0;
		z-index: 1200;
		display: grid;
		place-items: center;
		align-content: center;
		gap: clamp(1rem, 3vw, 1.8rem);
		background: #050505;
		pointer-events: none;
		animation: launch-fade 2.7s ease-out forwards;
	}
	.brand-mark {
		width: clamp(7rem, 20vw, 12rem);
		aspect-ratio: 1;
		background: url('/favicon.svg') center / contain no-repeat;
		animation: orbit-turn 2.2s cubic-bezier(0.2, 0.75, 0.25, 1) forwards;
	}
	.brand-lockup {
		display: grid;
		justify-items: center;
		animation: mark-arrive 2.7s ease-out forwards;
	}
	/* Anybody 800 at width 150, ALL CAPS: the maintainer's blind pick on Thu 10 Sep 2026 to sit
	   beside the terracotta mark. A 1.6 KB static instance subset to the wordmark's glyphs,
	   shipped in static/ so the launch stays offline. Every candidate and round is kept in
	   docs/brand/wordmark-font/. */
	@font-face {
		font-family: 'Anybody Wordmark';
		src: url('/fonts/anybody-800-w150-wordmark.woff2') format('woff2');
		font-weight: 800;
		font-display: block;
	}
	.brand-name {
		color: #fff;
		font-family: 'Anybody Wordmark', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
		font-size: clamp(1.7rem, 4.2vw, 2.9rem);
		font-weight: 800;
		letter-spacing: -0.01em;
		text-transform: uppercase;
	}
	/* The end rotation MUST be a multiple of 360deg. The two halves of the mark
	   carry different shades, so any other terminal angle settles on a mark
	   whose colors are swapped relative to the shipped artwork. The old value
	   was 900deg, which is 180deg out, and it went unnoticed because the rings
	   this replaced were their own artwork and had nothing to be wrong about.
	   Same eased shape as before, scaled from 900 to 1080. */
	@keyframes orbit-turn {
		0% { transform: rotate(0deg) scale(0.78); }
		35% { transform: rotate(648deg) scale(1.16); }
		70% { transform: rotate(984deg) scale(1.04); }
		100% { transform: rotate(1080deg) scale(1); }
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
		.brand-launch, .brand-mark, .brand-lockup { animation-duration: 1ms; }
	}
</style>
