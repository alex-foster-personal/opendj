<script lang="ts">
	/**
	 * Vertical channel fader (blue/orange line thumb, SCREENSHOT-SPEC 4).
	 * Fills its parent height. Real control: click/drag anywhere on the track.
	 * Optional thin green→red meter pulse from the live deck analyser.
	 * It now renders through ChannelLevelMeter as ten discrete segments.
	 */
	import { FADER_THUMB_H, faderValueFromPress } from '$lib/rb/fader-press-geometry';
	import AnimRunningDot from './AnimRunningDot.svelte';
	import { createShiftSelectPress } from '$lib/rb/shift-select-press';
	import ChannelLevelMeter from './ChannelLevelMeter.svelte';
	import FaderRejoinGuidance from './FaderRejoinGuidance.svelte';
	import {
		clearGhostIfReconciled,
		ghostThumbTopPx,
		guidanceVisible,
		onFaderDragEnd,
		onFaderPointerLeave,
		onFaderPointerMove,
		onWheelFaderAdjust,
		rejoinGlowActive
	} from '$lib/rb/fader-ghost.svelte';
	import { WHEEL_STEP, wheelAdjust } from '$lib/rb/wheel-adjust';
	import type { DeckId } from '$lib/rb/deck-slots';
	import { midiTakeoverGhost } from '$lib/rb/midi/takeover-ui.svelte';
	import {
		commandedMoveLanding,
		commandedMovePresentation
	} from '$lib/rb/commanded-move-runner.svelte';
	import {
		COMMANDED_GHOST_VARIANT,
		COMMANDED_SETTLE_VARIANT,
		FADER_SETTLE_PX,
		commandedGhostStyle,
		commandedSettleStyle,
		commandedSettleVars
	} from '$lib/rb/commanded-move-presentation';
	import { prefersReducedMotion } from '$lib/motion/reduced-motion';
	import { readVariant } from '$lib/rb/prefs.svelte';
	import { valueText } from '$lib/a11y/value-text';
	import {
		KNOB_CFG,
		beginSelectionDrag,
		faderControlId,
		isKnobSelected,
		nudgeKnobOrSelection,
		noteKnobHandInput,
		registerKnob,
		selectedKnobIds,
		setKnobHovered,
		shiftClickKnob,
		stepSelectionDrag,
		unregisterKnob,
		type SelectionDragStart
	} from '$lib/rb/knob-control.svelte';
	import { createDragTracker, trackDragMove, type DragTracker } from '$lib/rb/drag-sensitivity.svelte';

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
	// The hover names the fader and says which chevron is which (pin 561f92dd6c12).
	const faderTitle = $derived(`${label}. Up chevron is louder, down chevron is quieter. Drag to set the level.`);

	const THUMB_H = FADER_THUMB_H;

	let trackEl: HTMLDivElement | undefined = $state();
	let trackH = $state(96);
	let dragging = false;
	let pointerOnFader = false;
	let dragReleaseValue = value;
	/** Unclamped value the single-fader drag has integrated to (R5). */
	let dragVirtualValue = value;
	/** Gained (R5) pointer travel since the press. */
	let dragTracker: DragTracker | null = null;
	/** Non-null while a drag on this fader moves a 2+ control selection. */
	let selectionDrag: SelectionDragStart | null = null;

	const controlId = $derived(faderControlId(deckId));
	const selected = $derived(isKnobSelected(controlId));

	const thumbTopPx = $derived((1 - value) * Math.max(1, trackH - THUMB_H));
	const takeoverGhost = $derived(midiTakeoverGhost(`mixer:${deckId}:fader`));
	const takeoverGhostTopPx = $derived(
		takeoverGhost === null ? null : (1 - takeoverGhost.value) * Math.max(1, trackH - THUMB_H)
	);
	const ghostTopPx = $derived(ghostThumbTopPx(deckId, trackH, THUMB_H));
	// D6 (#9717): a commanded move's target mark; the fill is its progress. Display only.
	const commanded = $derived(commandedMovePresentation(controlId));
	const commandedStyle = $derived(commandedGhostStyle(readVariant(COMMANDED_GHOST_VARIANT.id)));
	const commandedTopPx = $derived(
		commanded === null ? null : (1 - commanded.target) * Math.max(1, trackH - THUMB_H)
	);
	// #14036: a landed move's settle on the thumb, a CSS keyframe only; never the value or aria.
	// A rising value moves the thumb up (negative y), so the overshoot side is flipped.
	const landing = $derived(commandedMoveLanding(controlId));
	const settleVars = $derived(
		landing === null
			? null
			: commandedSettleVars(FADER_SETTLE_PX, landing.direction === 1 ? -1 : 1, 'px', {
					reducedMotion: prefersReducedMotion(),
					style: commandedSettleStyle(readVariant(COMMANDED_SETTLE_VARIANT.id))
				})
	);
	const showGuidance = $derived(guidanceVisible(deckId));
	const rejoinGlow = $derived(rejoinGlowActive(deckId));

	function _clamp01(v: number): number {
		return Math.min(1, Math.max(0, v));
	}

	function _valueFromEvent(e: PointerEvent): number {
		const el = trackEl;
		if (!el) return value;
		return faderValueFromPress(el.getBoundingClientRect(), e.clientY, THUMB_H);
	}

	const shiftSelect = createShiftSelectPress();

	function handleClick(e: MouseEvent): void {
		// The strip listens for click (MIXUX-08 deck select). Stopping
		// pointerdown does not stop that later click, so only the shift
		// case is stopped here, keyed on the press as well as the click
		// (Shift may be up by pointerup). A plain click still selects the deck.
		if (!shiftSelect.click(e.shiftKey)) return;
		e.stopPropagation();
	}

	function handlePointerDown(e: PointerEvent): void {
		// Primary button only, like the knob: a right or middle press must not
		// toggle the selection or drag the group under a context menu.
		if (e.button !== 0) return;
		shiftSelect.press(e.shiftKey);
		// Shift toggles this fader in the knob selection and does not move it.
		if (e.shiftKey) {
			e.preventDefault();
			e.stopPropagation();
			shiftClickKnob(controlId);
			return;
		}
		dragging = true;
		pointerOnFader = true;
		dragTracker = createDragTracker(e.clientX, e.clientY);
		selectionDrag = beginSelectionDrag(controlId);
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		if (selectionDrag !== null) return;
		// The press still jumps the thumb to the pointer; travel after it is
		// relative, through the shared R5 gain and distance falloff.
		const next = _valueFromEvent(e);
		dragReleaseValue = next;
		dragVirtualValue = next;
		onchange(next);
	}

	function handlePointerMove(e: PointerEvent): void {
		onFaderPointerMove(deckId, e.clientY, value, thumbTopPx, trackH);
		if (!dragging) return;
		if (dragTracker === null) return;
		// One track height of gained travel is a full 0..1 sweep.
		const step = trackDragMove(dragTracker, e.clientX, e.clientY);
		const delta = step.dy / Math.max(1, trackH - THUMB_H);
		if (selectionDrag !== null) {
			// This fader leads the group (R2); Opt held flips one side (R3).
			stepSelectionDrag(selectionDrag, delta, e.altKey);
			return;
		}
		dragVirtualValue += delta;
		const next = _clamp01(dragVirtualValue);
		dragReleaseValue = next;
		noteKnobHandInput(controlId);
		onchange(next);
	}

	function handlePointerUp(e: PointerEvent): void {
		if (!dragging) return;
		const wasSelection = selectionDrag !== null;
		dragging = false;
		pointerOnFader = false;
		selectionDrag = null;
		dragTracker = null;
		// A selection move is not a pointer-to-value map, so it must not open a ghost.
		onFaderDragEnd(deckId, wasSelection ? value : dragReleaseValue, value);
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
	}

	function handlePointerEnter(): void {
		pointerOnFader = true;
		// The page-level wheel's lead (R3) is the control under the pointer.
		setKnobHovered(controlId);
	}

	function handlePointerLeave(): void {
		if (!dragging) pointerOnFader = false;
		setKnobHovered(null);
		onFaderPointerLeave(deckId);
	}

	function _inGroupSelection(): boolean {
		return isKnobSelected(controlId) && selectedKnobIds().length >= 2;
	}

	function handleKeyDown(e: KeyboardEvent): void {
		const up = e.key === 'ArrowUp';
		const down = e.key === 'ArrowDown';
		if (!up && !down) return;
		e.preventDefault();
		const delta = up ? KNOB_CFG.keyStep : -KNOB_CFG.keyStep;
		noteKnobHandInput(controlId);
		if (_inGroupSelection()) {
			nudgeKnobOrSelection(controlId, delta);
			return;
		}
		const before = value;
		const next = _clamp01(before + delta);
		onWheelFaderAdjust(deckId, before, next, false);
		onchange(next);
	}

	function handleWheelAdjust(next: number): void {
		noteKnobHandInput(controlId);
		onWheelFaderAdjust(deckId, value, next, pointerOnFader);
		onchange(next);
	}

	/** Raw step when this fader is in a 2+ selection, so a pin at 0 or 1 still moves the rest. */
	function handleWheelNudge(delta: number, event: WheelEvent): void {
		if (_inGroupSelection()) {
			nudgeKnobOrSelection(controlId, delta, event.altKey);
			return;
		}
		handleWheelAdjust(_clamp01(value + delta));
	}

	$effect(() => {
		clearGhostIfReconciled(deckId, value);
	});

	$effect(() => {
		const id = controlId;
		registerKnob({ id, getValue: () => value, setValue: (next) => onchange(next) });
		return () => unregisterKnob(id);
	});

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
	class:rb-fader-rejoin={rejoinGlow}
	class:fader-selected={selected}
	bind:this={trackEl}
	use:wheelAdjust={{
		step: WHEEL_STEP.fader,
		get: () => value,
		set: handleWheelAdjust,
		nudge: handleWheelNudge
	}}
	role="slider"
	aria-label={label}
	title={faderTitle}
	data-testid={`channel-${deckId}-fader`}
	data-knob-id={controlId}
	data-takeover-ghost={takeoverGhost === null ? undefined : takeoverGhost.value}
	data-commanded-target={commanded?.target}
	data-commanded-progress={commanded?.progress}
	data-commanded-settle={settleVars === null ? undefined : landing?.direction}
	aria-orientation="vertical"
	aria-valuemin={0}
	aria-valuemax={1}
	aria-valuenow={value}
	aria-valuetext={valueText('fader', value)}
	tabindex="0"
	onclick={handleClick}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	onpointerenter={handlePointerEnter}
	onpointerleave={handlePointerLeave}
	onkeydown={handleKeyDown}
>
	<!-- Pin 561f92dd6c12: these marks are the drag direction, not buttons.
	     The word sits with the chevron so a hover answers which end is which. -->
	<span class="fader-chevron fader-chevron-up" aria-hidden="true">
		<svg viewBox="0 0 8 5" width="8" height="5">
			<path d="M1 4 L4 1 L7 4" fill="none" stroke="currentColor" stroke-width="1.3" />
		</svg>
		<span class="fader-chevron-caption">louder</span>
	</span>
	<span class="fader-chevron fader-chevron-down" aria-hidden="true">
		<span class="fader-chevron-caption">quieter</span>
		<svg viewBox="0 0 8 5" width="8" height="5">
			<path d="M1 1 L4 4 L7 1" fill="none" stroke="currentColor" stroke-width="1.3" />
		</svg>
	</span>
	<div class="rb-fader-track"></div>
	<ChannelLevelMeter {deckId} {playing} />
	{#if ghostTopPx !== null}
		<div class="rb-fader-ghost-thumb" style={`top: ${ghostTopPx}px;`}></div>
	{:else if takeoverGhostTopPx !== null}
		<div class="rb-fader-ghost" style={`top: ${takeoverGhostTopPx}px;`}></div>
	{/if}
	{#if commanded !== null && commandedTopPx !== null}
		<div
			class="rb-fader-commanded-target"
			data-testid={`commanded-ghost-${controlId}`}
			style={`top: ${commandedTopPx}px; height: ${THUMB_H}px;`}
		>
			{#if commandedStyle === 'ghost-arc'}
				<div
					class="rb-fader-commanded-progress"
					data-testid={`commanded-progress-${controlId}`}
					style={`width: ${(commanded.progress * 100).toFixed(1)}%;`}
				></div>
			{/if}
		</div>
	{/if}
	{#key landing?.seq}
		<div
			class="rb-fader-thumb"
			class:commanded-settling={settleVars !== null}
			data-testid={settleVars === null ? undefined : `commanded-settle-${controlId}`}
			style={`top: ${thumbTopPx}px;${settleVars === null ? '' : ` ${settleVars}`}`}
		></div>
	{/key}
	<AnimRunningDot animKey={controlId} />
	{#if showGuidance && ghostTopPx !== null}
		<FaderRejoinGuidance fromY={ghostTopPx + THUMB_H / 2} toY={thumbTopPx + THUMB_H / 2} trackWidth={30} />
	{/if}
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
	.rb-fader.fader-selected .rb-fader-thumb {
		box-shadow: 0 0 0 1px var(--rb-accent);
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

	.rb-fader-commanded-target {
		position: absolute;
		left: 2px;
		right: 2px;
		box-sizing: border-box;
		border: 1px dashed var(--rb-accent);
		border-radius: 2px;
		opacity: 0.7;
		pointer-events: none;
	}
	.rb-fader-thumb.commanded-settling {
		animation: commanded-settle-fader var(--settle-ms) linear;
	}
	@keyframes commanded-settle-fader {
		0% {
			transform: translateY(var(--settle-0));
		}
		25% {
			transform: translateY(var(--settle-1));
		}
		50% {
			transform: translateY(var(--settle-2));
		}
		75% {
			transform: translateY(var(--settle-3));
		}
		100% {
			transform: translateY(var(--settle-4));
		}
	}
	.rb-fader-commanded-progress {
		height: 100%;
		background: var(--rb-accent);
		opacity: 0.45;
		pointer-events: none;
	}

	.rb-fader:focus-visible {
		/* A11Y-11403: keyboard focus ring paired with this file's outline reset. */
		outline: 2px solid var(--rb-focus-ring);
		outline-offset: 2px;
	}

	/* A11Y-11811: reduced motion (OS or Appearance); mirrors app.css, as Knob.svelte does. */
	@media (prefers-reduced-motion: reduce) {
		:where(*) {
			animation-duration: 0.01ms;
			animation-iteration-count: 1;
			transition-duration: 0.01ms;
		}
	}
	:global(html[data-reduce-motion]) :where(*) {
		animation-duration: 0.01ms;
		animation-iteration-count: 1;
		transition-duration: 0.01ms;
	}
</style>
