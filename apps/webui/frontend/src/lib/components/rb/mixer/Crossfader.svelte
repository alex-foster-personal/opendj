<script lang="ts">
	/**
	 * Horizontal crossfader (SCREENSHOT-SPEC 4, bottom center of the mixer).
	 * Real control: 0 = full A (left bus), 1 = full B (right bus).
	 * Wheel up moves toward B.
	 */
	import {
		CROSSFADER_THUMB_HIT_W,
		CROSSFADER_THUMB_VIS_H,
		CROSSFADER_THUMB_VIS_W,
		crossfaderThumbLeftPx,
		crossfaderValueFromPointerX
	} from '$lib/rb/crossfader-geometry';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import { midiTakeoverGhost } from '$lib/rb/midi/takeover-state.svelte';

	interface Props {
		/** 0..1; 0 = full A, 1 = full B. */
		value: number;
		/** Called with the new 0..1 value while dragging. */
		onchange: (value: number) => void;
	}

	let { value, onchange }: Props = $props();

	let dragging = false;
	let trackWidth = $state(0);

	const thumbLeftPx = $derived(crossfaderThumbLeftPx(value, trackWidth));
	const takeoverGhost = $derived(midiTakeoverGhost('mixer:global:crossfader'));
	const ghostLeftPx = $derived(
		takeoverGhost === null ? 0 : crossfaderThumbLeftPx(takeoverGhost.value, trackWidth)
	);

	function _clamp01(v: number): number {
		return Math.min(1, Math.max(0, v));
	}

	function _valueFromEvent(e: PointerEvent): number {
		const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
		return crossfaderValueFromPointerX(e.clientX, rect.left, rect.width);
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
		if (e.key === 'ArrowRight') {
			e.preventDefault();
			onchange(_clamp01(value + 0.02));
		} else if (e.key === 'ArrowLeft') {
			e.preventDefault();
			onchange(_clamp01(value - 0.02));
		}
	}
</script>

<div
	class="xfader"
	role="slider"
	aria-label="crossfader"
	title="Crossfader - blend channels assigned to A (left) and B (right). Center is equal mix of both buses."
	aria-orientation="horizontal"
	aria-valuemin={0}
	aria-valuemax={1}
	aria-valuenow={value}
	tabindex="0"
	data-takeover-ghost={takeoverGhost === null ? undefined : takeoverGhost.value}
	bind:clientWidth={trackWidth}
	use:wheelAdjust={{ step: WHEEL_STEP.crossfader, get: () => value, set: onchange }}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	onkeydown={handleKeyDown}
>
	<div class="track"></div>
	{#if takeoverGhost !== null}
		<div class="ghost-thumb" style={`left: ${ghostLeftPx}px;`}></div>
	{/if}
	<div class="thumb-hit" style={`left: ${thumbLeftPx}px; width: ${CROSSFADER_THUMB_HIT_W}px;`}>
		<div
			class="thumb-visual"
			style={`width: ${CROSSFADER_THUMB_VIS_W}px; height: ${CROSSFADER_THUMB_VIS_H}px;`}
		></div>
	</div>
</div>

<style>
	.xfader {
		position: relative;
		flex: 1;
		height: 22px;
		cursor: ew-resize;
		touch-action: none;
		outline: none;
	}
	.track {
		position: absolute;
		left: 0;
		right: 0;
		top: 50%;
		height: 3px;
		margin-top: -1.5px;
		background: #060809;
		border: 1px solid var(--rb-border);
	}
	.thumb-hit {
		position: absolute;
		top: 0;
		bottom: 0;
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.thumb-visual {
		background: #2a2f37;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		position: relative;
	}
	.ghost-thumb {
		position: absolute;
		top: 2px;
		bottom: 2px;
		width: 12px;
		border: 1px dashed #f2b84b;
		border-radius: 2px;
		pointer-events: none;
	}
	.thumb-visual::after {
		content: '';
		position: absolute;
		top: 2px;
		bottom: 2px;
		left: 50%;
		width: 2px;
		margin-left: -1px;
		background: var(--rb-accent);
	}
	.xfader:focus-visible .thumb-visual {
		box-shadow: 0 0 4px var(--rb-accent-glow);
	}
</style>
