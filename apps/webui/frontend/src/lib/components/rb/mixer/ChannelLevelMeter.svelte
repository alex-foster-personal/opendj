<script lang="ts">
	/**
	 * Discrete post-deck, pre-channel-fader channel level meter.
	 *
	 * The live reading stays on the Web Audio analyser so it updates on each
	 * animation frame without involving Python or Rust. Red indicates digital
	 * clipping risk at this signal point, not speaker-damage calibration.
	 */
	import { onDestroy } from 'svelte';
	import { peekDeckMeter } from '$lib/rb/audio-engine.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';

	interface Props {
		deckId: DeckId;
		playing?: boolean;
	}

	let { deckId, playing = false }: Props = $props();

	const SEGMENTS = [
		'green',
		'green',
		'green',
		'green',
		'green',
		'green',
		'yellow',
		'yellow',
		'red',
		'red'
	] as const;

	let meter = $state(0);
	let raf = 0;
	const activeSegmentCount = $derived(Math.ceil(meter * SEGMENTS.length));

	$effect(() => {
		const live = playing;
		cancelAnimationFrame(raf);
		if (!live) {
			meter = 0;
			return;
		}
		const tick = (): void => {
			meter = peekDeckMeter(deckId);
			raf = requestAnimationFrame(tick);
		};
		raf = requestAnimationFrame(tick);
		return () => cancelAnimationFrame(raf);
	});

	onDestroy(() => cancelAnimationFrame(raf));
</script>

<div
	class="rb-channel-level-meter"
	role="meter"
	aria-label={`channel ${deckId} level, post-deck pre-fader`}
	aria-valuemin={0}
	aria-valuemax={1}
	aria-valuenow={meter}
	title="Post-deck, pre-channel-fader level. Red signals digital clipping risk."
>
	{#each SEGMENTS as color, index}
		<div
			class:lit={index < activeSegmentCount}
			class:green={color === 'green'}
			class:yellow={color === 'yellow'}
			class:red={color === 'red'}
			class="rb-channel-level-meter-segment"
			aria-hidden="true"
		></div>
	{/each}
</div>

<style>
	.rb-channel-level-meter {
		position: absolute;
		left: 50%;
		bottom: 0;
		z-index: 1;
		display: flex;
		flex-direction: column-reverse;
		gap: 2px;
		width: 4px;
		height: 100%;
		transform: translateX(-50%);
		pointer-events: none;
	}
	.rb-channel-level-meter-segment {
		min-height: 2px;
		flex: 1;
		border-radius: 1px;
		opacity: 0.18;
	}
	.rb-channel-level-meter-segment.green {
		background: #35c04f;
	}
	.rb-channel-level-meter-segment.yellow {
		background: #e8a13a;
	}
	.rb-channel-level-meter-segment.red {
		background: #e23a32;
	}
	.rb-channel-level-meter-segment.lit {
		opacity: 0.85;
		box-shadow: 0 0 3px currentColor;
	}
	.rb-channel-level-meter-segment.green.lit {
		color: #35c04f;
	}
	.rb-channel-level-meter-segment.yellow.lit {
		color: #e8a13a;
	}
	.rb-channel-level-meter-segment.red.lit {
		color: #e23a32;
	}
</style>
