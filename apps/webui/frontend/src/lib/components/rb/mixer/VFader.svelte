<script lang="ts">
	/**
	 * Vertical channel fader (blue/orange line thumb, SCREENSHOT-SPEC 4).
	 * Fills its parent height. Real control: click/drag anywhere on the track.
	 * Optional thin green→red meter pulse from the live deck analyser.
	 * It now renders through ChannelLevelMeter as ten discrete segments.
	 */
	import ChannelLevelMeter from './ChannelLevelMeter.svelte';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import type { DeckId } from '$lib/rb/deck-slots';
	import { midiTakeoverGhost } from '$lib/rb/midi/takeover-state.svelte';

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
	let dragging = false;

	const thumbTopPx = $derived((1 - value) * Math.max(1, trackH - THUMB_H));
	const takeoverGhost = $derived(midiTakeoverGhost(`mixer:${deckId}:fader`));
	const ghostTopPx = $derived(
		takeoverGhost === null ? 0 : (1 - takeoverGhost.value) * Math.max(1, trackH - THUMB_H)
	);

	function _clamp01(v: number): number {
		return Math.min(1, Math.max(0, v));
	}

	function _valueFromEvent(e: PointerEvent): number {
		const el = trackEl;
		if (!el) return value;
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
		if (!el) return;
		const ro = new ResizeObserver((entries) => {
			trackH = Math.max(1, Math.round(entries[0].contentRect.height));
		});
		ro.observe(el);
		trackH = Math.max(1, Math.round(el.getBoundingClientRect().height));
		return () => ro.disconnect();
	});

</script>

<div
	class="rb-fader"
	class:playing
	class:looped
	bind:this={trackEl}
	use:wheelAdjust={{ step: WHEEL_STEP.fader, get: () => value, set: onchange }}
	role="slider"
	aria-label={label}
	title={label}
	data-testid={`channel-${deckId}-fader`}
	data-takeover-ghost={takeoverGhost === null ? undefined : takeoverGhost.value}
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
	<ChannelLevelMeter {deckId} {playing} />
	{#if takeoverGhost !== null}
		<div class="rb-fader-ghost" style={`top: ${ghostTopPx}px;`}></div>
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
	.rb-fader-ghost {
		position: absolute;
		left: 2px;
		right: 2px;
		height: 10px;
		border: 1px dashed #f2b84b;
		border-radius: 2px;
		pointer-events: none;
	}
</style>
