<script lang="ts">
	/**
	 * Vertical channel fader (blue/orange line thumb, SCREENSHOT-SPEC 4).
	 * Fills its parent height. Real control: click/drag anywhere on the track.
	 * Optional thin green→red meter pulse from the live deck analyser.
	 */
	import { onDestroy } from 'svelte';
	import { peekDeckMeter } from '$lib/rb/audio-engine.svelte';
	import type { DeckId } from '$lib/rb/types';

	interface Props {
		/** 0..1; 1 = full (thumb at top). */
		value: number;
		/** Called with the new 0..1 value while dragging. */
		onchange: (value: number) => void;
		/** Accessible name, e.g. 'channel 3 fader'. */
		label: string;
		/** Deck id for live meter sampling. */
		deckId: DeckId;
		/** True while the deck transport is playing - animates the thumb line. */
		playing?: boolean;
		/** True while an engaged loop is active - tints the thumb line orange. */
		looped?: boolean;
	}

	let { value, onchange, label, deckId, playing = false, looped = false }: Props = $props();

	const THUMB_H = 12;

	let trackEl: HTMLDivElement | undefined = $state();
	let trackH = $state(96);
	let meter = $state(0);
	let dragging = false;
	let raf = 0;

	const thumbTopPx = $derived((1 - value) * Math.max(1, trackH - THUMB_H));

	function _clamp01(v: number): number {
		return Math.min(1, Math.max(0, v));
	}

	function _valueFromEvent(e: PointerEvent): number {
		const el = trackEl;
		if (el === undefined) return value;
		const rect = el.getBoundingClientRect();
		const y = e.clientY - rect.top - THUMB_H / 2;
		return _clamp01(1 - y / Math.max(1, rect.height - THUMB_H));
	}

	function handlePointerDown(e: PointerEvent): void {
		dragging = true;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		onchange(_valueFromEvent(e));
	}

	function handlePointerMove(e: PointerEvent): void {
		if (!dragging) return;
		onchange(_valueFromEvent(e));
	}

	function handlePointerUp(e: PointerEvent): void {
		if (!dragging) return;
		dragging = false;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
	}

	function handleKeyDown(e: KeyboardEvent): void {
		if (e.key === 'ArrowUp') {
			e.preventDefault();
			onchange(_clamp01(value + 0.02));
		} else if (e.key === 'ArrowDown') {
			e.preventDefault();
			onchange(_clamp01(value - 0.02));
		}
	}

	$effect(() => {
		const el = trackEl;
		if (el === undefined) return;
		const ro = new ResizeObserver((entries) => {
			trackH = Math.max(1, Math.round(entries[0].contentRect.height));
		});
		ro.observe(el);
		trackH = Math.max(1, Math.round(el.getBoundingClientRect().height));
		return () => ro.disconnect();
	});

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
	class="rb-fader"
	class:playing
	class:looped
	bind:this={trackEl}
	role="slider"
	aria-label={label}
	aria-orientation="vertical"
	aria-valuemin={0}
	aria-valuemax={1}
	aria-valuenow={value}
	tabindex="0"
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	onkeydown={handleKeyDown}
>
	<div class="rb-fader-track"></div>
	{#if meter > 0.02}
		<div
			class="rb-fader-meter"
			style={`height:${Math.round(meter * 100)}%;`}
			aria-hidden="true"
		></div>
	{/if}
	<div class="rb-fader-thumb" style={`top: ${thumbTopPx}px;`}></div>
</div>

<style>
	.rb-fader {
		cursor: ns-resize;
		touch-action: none;
		outline: none;
		height: 100%;
		width: 30px;
		position: relative;
	}
	.rb-fader:focus-visible .rb-fader-thumb {
		box-shadow: 0 0 4px var(--rb-accent-glow);
	}
	.rb-fader-meter {
		position: absolute;
		left: 50%;
		bottom: 0;
		width: 3px;
		margin-left: -1.5px;
		pointer-events: none;
		z-index: 1;
		border-radius: 1px;
		background: linear-gradient(
			to top,
			#35c04f 0%,
			#35c04f 55%,
			#e8a13a 78%,
			#e23a32 100%
		);
		opacity: 0.55;
		box-shadow: 0 0 4px rgba(226, 58, 50, 0.25);
	}
</style>
