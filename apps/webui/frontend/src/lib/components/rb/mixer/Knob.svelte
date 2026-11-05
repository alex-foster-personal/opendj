<script lang="ts">
	/**
	 * Rotary knob: SVG dark ring + indicator line (SCREENSHOT-SPEC 6).
	 * Real knobs: drag-vertical to turn, double-click resets to resetValue
	 * (0.5 for EQ/filter, 0 for CUE<>MASTER mix), arrow keys nudge, mouse
	 * wheel nudges. Inert knobs render identically but
	 * ignore input and carry the standard tooltip.
	 *
	 * All input routes through $lib/rb/knob-control (H5, MIXUX-13): shift+click
	 * toggles a dial in an ordered selection set that the global scroll wheel,
	 * arrow keys and a drag led by the dragged dial move together, alt+click links two dials so one turn moves them inversely with the
	 * rising side over-boosted by KNOB_CFG.linkStagger - which is what stops the
	 * bass dipping through a crossover. Sensitivity lives in KNOB_CFG, not here,
	 * so horizontal drag stays the fine-adjust axis.
	 */
	import AnimRunningDot from './AnimRunningDot.svelte';
	import { createShiftSelectPress } from '$lib/rb/shift-select-press';
	import { fitKnobCaption } from '$lib/rb/knob-caption-fit';
	import { onDestroy, type Snippet } from 'svelte';
	import { createDeferredClickGuard } from '$lib/rb/deferred-click';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import {
		KNOB_CFG,
		altClickKnob,
		beginSelectionDrag,
		clamp01,
		isKnobLinked,
		isKnobSelected,
		linkedPartnerId,
		nudgeKnobOrSelection,
		pointerTravelIsDrag,
		readKnobValue,
		registerKnob,
		setKnobAbsolute,
		singleDragDelta,
		setKnobFromDrag,
		setKnobHovered,
		shiftClickKnob,
		stepSelectionDrag,
		unregisterKnob,
		type SelectionDragStart
	} from '$lib/rb/knob-control.svelte';
	import { createDragTracker, trackDragMove, type DragTracker } from '$lib/rb/drag-sensitivity.svelte';
	import { wheelAdjust } from '$lib/rb/wheel-adjust';
	import { midiTakeoverGhost } from '$lib/rb/midi/takeover-ui.svelte';
	import { DIAL_SWEEP_DEG } from '$lib/rb/dial-clock';

	interface Props {
		/** Stable control id from knobId(deckId, role) - the knob-control registry key. */
		knobId: string;
		/** Small caps label under the knob (TRIM / HI / MID / LOW / FILTER ...). */
		label: string;
		/** AX name when the visible caption needs deck context. */
		accessibleLabel?: string;
		/** Position 0..1; 0.5 = center detent (unity / flat). */
		value: number;
		/** Called with the new 0..1 value on every drag/keyboard change. */
		onchange?: (value: number) => void;
		/** True = visually authentic but ignores all input. */
		inert?: boolean;
		/** Indicator stroke tone. */
		tone?: 'accent' | 'white' | 'rainbow';
		/** Rendered dial diameter in px. The caption-inclusive wrapper is the hit area. */
		size?: number;
		/** When set, indicator and label use this color and EQ warn overlays are suppressed. */
		accentColor?: string;
		/** Double-click reset. EQ/filter stay at the 0.5 detent; MIX is 0 (full cue). */
		resetValue?: number;
		/** Optional delayed single-click action (suppressed by drag and double-click). */
		onsingleclick?: () => void;
		/** Optional mark rendered inside the caption, before the label text ("[icon] MIX"). */
		captionLead?: Snippet;
		/**
		 * COLORFX-10: immediate tap action. Fires on pointerup when the press
		 * stayed inside the click slop (a drag still turns the dial), and on
		 * Enter / Space. Unlike `onsingleclick` it is not deferred for a
		 * double-click, so a menu opens on the first click.
		 */
		ontap?: () => void;
		/** Optional in-flow mark after the label text (the color FX chevron). */
		captionTrail?: Snippet;
		/** Width of `captionTrail` incl. its gap, px, so the caption fit counts it. */
		captionTrailPx?: number;
		/** Set when `ontap` opens a menu: drives aria-haspopup and data-popup-open
		 * (role=slider does not support aria-expanded). */
		popupOpen?: boolean;
	}

	let {
		knobId,
		label,
		accessibleLabel,
		value,
		onchange,
		inert = false,
		tone = 'accent',
		size = 30,
		accentColor,
		resetValue = 0.5,
		onsingleclick,
		captionLead,
		ontap,
		captionTrail,
		captionTrailPx = 0,
		popupOpen
	}: Props = $props();

	const SWEEP_DEG = DIAL_SWEEP_DEG; // -135deg .. +135deg like rekordbox knobs (7:30 to 4:30, DIALCLOCK-8341)
	const RAINBOW_STOPS = ['#e23a32', '#e8a13a', '#d7d83a', '#35c04f'] as const;
	const visualStyle = $derived.by(() => {
		if (!Number.isFinite(size) || size <= 0) {
			throw new Error(`Knob size must be a positive finite number, got ${size}`);
		}
		const caption = fitKnobCaption(label, size, captionTrail ? captionTrailPx : 0);
		return (
			`--knob-dial-size: ${size}px; ` +
			`--knob-caption-size: ${caption.fontPx}px; ` +
			`--knob-caption-max: ${caption.maxWidthPx}px; ` +
			`--knob-caption-gap: 1px; ` +
			(accentColor ? `--knob-accent-color: ${accentColor}; ` : '') +
			`--knob-rainbow-0: ${RAINBOW_STOPS[0]}; ` +
			`--knob-rainbow-1: ${RAINBOW_STOPS[1]}; ` +
			`--knob-rainbow-2: ${RAINBOW_STOPS[2]}; ` +
			`--knob-rainbow-3: ${RAINBOW_STOPS[3]};`
		);
	});

	const live = $derived(!inert && onchange !== undefined);

	let dragStartX = 0;
	let dragStartY = 0;
	let dragStartValue = 0;
	/** Gained (R5) pointer travel, integrated since the press. */
	let dragTracker: DragTracker | null = null;
	let dragSumDx = 0;
	let dragSumDy = 0;
	let dragPartnerId: string | null = null;
	let dragStartPartnerValue: number | null = null;
	let dragging = false;
	/**
	 * Needle position while a drag is down. The committed `value` can lag a
	 * frame (stem EQ commits through stem_gain), and without this the dial
	 * looks stuck under the pointer.
	 */
	let dragShown = $state<number | null>(null);
	/** Non-null while a drag on a selected dial moves the whole 2+ selection. */
	let selectionDrag: SelectionDragStart | null = null;
	let pointerMoved = false;
	/** Alt pressed on a grouped dial: a flip drag, or a link click if it never moves. */
	let altPressInGroup = false;
	const singleClickGuard = createDeferredClickGuard();

	onDestroy(() => {
		singleClickGuard.dispose();
	});

	$effect(() => {
		if (!live) return;
		registerKnob({ id: knobId, getValue: () => value, setValue: (next) => onchange?.(next) });
		return () => unregisterKnob(knobId);
	});

	const selected = $derived(live && isKnobSelected(knobId));
	const linked = $derived(live && isKnobLinked(knobId));
	const takeoverFunction = $derived.by(() => {
		const [scope, role] = knobId.split(':');
		if (scope === 'hp' && role === 'hp-mix') return 'headphones:mix';
		if (scope === 'hp' && role === 'hp-level') return 'headphones:level';
		if (!/^[1-4]$/.test(scope)) return null;
		if (role === 'trim' || role === 'filter') return `mixer:${scope}:${role}`;
		if (role === 'high' || role === 'mid' || role === 'low') return `mixer:${scope}:eq:${role}`;
		return null;
	});
	const takeoverGhost = $derived(takeoverFunction === null ? null : midiTakeoverGhost(takeoverFunction));
	const ghostAngleDeg = $derived(takeoverGhost === null ? 0 : (takeoverGhost.value - 0.5) * SWEEP_DEG);

	const displayValue = $derived(dragShown ?? value);
	const angleDeg = $derived((displayValue - 0.5) * SWEEP_DEG);
	/** |offset| from center: >0.15 (~30% of half-throw) orange, >0.25 (~50%) red. */
	const warn = $derived.by((): 'none' | 'orange' | 'red' => {
		if (accentColor) return 'none';
		const d = Math.abs(displayValue - 0.5);
		if (d > 0.25) return 'red';
		if (d > 0.15) return 'orange';
		return 'none';
	});

	const shiftSelect = createShiftSelectPress();

	function handleClick(e: MouseEvent): void {
		// A shift-click is control selection (R1), never MIXUX-08 deck select:
		// the strip listens for click, so stop only the shift case here, keyed
		// on the press as well as the click (Shift may be up by pointerup).
		if (!shiftSelect.click(e.shiftKey)) return;
		e.stopPropagation();
	}

	function handlePointerDown(e: PointerEvent): void {
		// Right and middle clicks belong to the context menu, not the dial.
		if (!live || e.button !== 0) return;
		shiftSelect.press(e.shiftKey);
		// Shift = select for the global wheel; Alt = arm/complete a link pair.
		// Neither starts a drag, so a modifier click never also turns the dial.
		if (e.shiftKey) {
			e.preventDefault();
			shiftClickKnob(knobId);
			return;
		}
		// Alt on a dial of a 2+ group starts a flipped drag (R3); released
		// without travel it is still the Alt-click that arms a link pair.
		altPressInGroup = e.altKey && isKnobSelected(knobId) && beginSelectionDrag(knobId) !== null;
		if (e.altKey && !altPressInGroup) {
			e.preventDefault();
			altClickKnob(knobId);
			return;
		}
		if (altPressInGroup) e.preventDefault();
		dragging = true;
		pointerMoved = false;
		singleClickGuard.cancel();
		dragStartX = e.clientX;
		dragStartY = e.clientY;
		dragStartValue = value;
		dragTracker = createDragTracker(e.clientX, e.clientY);
		dragSumDx = 0;
		dragSumDy = 0;
		// Baselines are captured ONCE so the link stagger cannot accumulate
		// across pointermove frames.
		selectionDrag = beginSelectionDrag(knobId);
		dragPartnerId = linkedPartnerId(knobId);
		dragStartPartnerValue = dragPartnerId === null ? null : readKnobValue(dragPartnerId);
		// A linked or grouped drag commits through the registry (stagger / the
		// other dials). Only a lone dial previews the pointer, or the preview
		// would hide that commit and stick.
		dragShown = selectionDrag === null && dragPartnerId === null ? value : null;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handlePointerMove(e: PointerEvent): void {
		if (!dragging || !live) return;
		if (!pointerMoved) {
			// Only a knob with a click action has a click to protect: jitter inside the
			// slop stays that click. Every other knob keeps moving from the first pixel.
			const insideSlop = !pointerTravelIsDrag(e.clientX - dragStartX, e.clientY - dragStartY);
			// An Alt press on a grouped dial is the same: inside the slop it is
			// still the link click.
			if ((onsingleclick !== undefined || ontap !== undefined || altPressInGroup) && insideSlop) return;
			pointerMoved = true;
		}
		if (dragTracker === null) return;
		// up = clockwise = increase, right = increase; both gained (R5).
		const step = trackDragMove(dragTracker, e.clientX, e.clientY);
		if (selectionDrag !== null) {
			// The dial under the pointer leads the whole group (R2).
			// Opt held right now flips one side (R3); read per frame, so a
			// release mid-drag only changes what comes next, never jumps.
			stepSelectionDrag(selectionDrag, singleDragDelta(step.dx, step.dy), e.altKey);
			return;
		}
		// One dial: horizontal is the fine-adjust axis.
		dragSumDx += step.dx;
		dragSumDy += step.dy;
		const target = clamp01(dragStartValue + singleDragDelta(dragSumDx, dragSumDy));
		// A linked drag commits with the stagger added, so it shows the
		// committed value, never the bare pointer target.
		if (dragPartnerId === null) dragShown = target;
		setKnobFromDrag(knobId, dragStartValue, target, dragPartnerId, dragStartPartnerValue);
	}

	function handlePointerUp(e: PointerEvent): void {
		if (!dragging) return;
		const wasDrag = pointerMoved;
		dragging = false;
		dragShown = null;
		selectionDrag = null;
		dragTracker = null;
		dragPartnerId = null;
		dragStartPartnerValue = null;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
		const altClick = altPressInGroup && !wasDrag;
		altPressInGroup = false;
		if (altClick) {
			altClickKnob(knobId);
			return;
		}
		if (!live || wasDrag) return;
		ontap?.();
		if (onsingleclick === undefined) return;
		singleClickGuard.schedule(() => onsingleclick?.());
	}

	function handleDblClick(): void {
		singleClickGuard.cancel();
		if (!live) return;
		setKnobAbsolute(knobId, resetValue);
	}

	function handleKeyDown(e: KeyboardEvent): void {
		if (!live) return;
		if (ontap !== undefined && (e.key === 'Enter' || e.key === ' ')) {
			e.preventDefault();
			ontap();
		} else if (e.key === 'ArrowUp' || e.key === 'ArrowRight') {
			e.preventDefault();
			nudgeKnobOrSelection(knobId, KNOB_CFG.keyStep);
		} else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') {
			e.preventDefault();
			nudgeKnobOrSelection(knobId, -KNOB_CFG.keyStep);
		}
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="knob"
	class:rb-inert={inert}
	class:knob-selected={selected}
	class:knob-linked={linked}
	class:warn-orange={warn === 'orange'}
	class:warn-red={warn === 'red'}
	class:knob-stem-accent={accentColor !== undefined}
	class:takeover-armed={takeoverGhost !== null}
	data-knob-id={knobId}
	data-testid={`knob-${knobId}`}
	data-takeover-ghost={takeoverGhost === null ? undefined : takeoverGhost.value}
	role="slider"
	aria-label={accessibleLabel ?? label}
	aria-valuemin={0}
	aria-valuemax={1}
	aria-valuenow={displayValue}
	aria-disabled={inert}
	aria-haspopup={popupOpen === undefined ? undefined : 'menu'}
	data-popup-open={popupOpen}
	tabindex={inert ? -1 : 0}
	style={visualStyle}
	onpointerenter={() => setKnobHovered(live ? knobId : null)}
	onpointerleave={() => setKnobHovered(null)}
	onclick={handleClick}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	onpointercancel={handlePointerUp}
	ondblclick={handleDblClick}
	onkeydown={handleKeyDown}
	use:wheelAdjust={{
		step: KNOB_CFG.scrollStep,
		get: () => value,
		// Through the registry, so a linked partner moves with it, and a 2+
		// selection moves together (MIXUX-13). The raw step, not a delta from
		// the clamped value, so a dial pinned at 0 or 1 still moves the rest.
		set: (next) => setKnobAbsolute(knobId, next),
		nudge: (delta, event) => nudgeKnobOrSelection(knobId, delta, event.altKey),
		disabled: !live
	}}
	title={inert
		? `${label}: ${plannedTitle('mixer-knob')}`
		: `${label}${selected ? ' - selected: the scroll wheel nudges every selected dial from anywhere; Esc clears' : ''}${linked ? ' - linked: turning this dial moves its partner the other way' : ''}`}
>
	<svg
		width={size}
		height={size}
		viewBox="0 0 30 30"
		aria-hidden="true"
	>
		<circle cx="15" cy="15" r="14" class="ring" />
		<circle cx="15" cy="15" r="11" class="cap" />
		<g style={`transform: rotate(${angleDeg}deg); transform-origin: 15px 15px;`}>
			<line
				x1="15"
				y1="5"
				x2="15"
				y2="12"
				class="indicator"
				class:white={tone === 'white'}
				class:rainbow={tone === 'rainbow'}
			/>
		</g>
		{#if takeoverGhost !== null}
			<g
				class="pickup-ghost-indicator"
				style={`transform: rotate(${ghostAngleDeg}deg); transform-origin: 15px 15px;`}
			>
				<line x1="15" y1="2.5" x2="15" y2="7" />
			</g>
		{/if}
	</svg>
	<AnimRunningDot animKey={knobId} />
	{#if takeoverGhost !== null}
		<span class="sr-only">Hardware at {(takeoverGhost.value * 100).toFixed(0)}%; move to {(takeoverGhost.target * 100).toFixed(0)}% to pick up.</span>
	{/if}
	<span class="label">{#if captionLead}<span class="label-lead">{@render captionLead()}</span>{/if}<span class="label-text">{label}</span>{#if captionTrail}{@render captionTrail()}{/if}</span>
</div>

<style>
	.knob {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--knob-caption-gap);
		width: max-content;
		min-width: var(--knob-dial-size);
		cursor: ns-resize;
		touch-action: none;
		outline: none;
	}
	.pickup-ghost-indicator line {
		stroke: var(--rb-knob-takeover, #f2b84b);
		stroke-width: 1.5;
		stroke-dasharray: 1.5 1;
	}
	.takeover-armed .ring { stroke: var(--rb-knob-takeover, #f2b84b); }
	.knob.warn-orange .cap {
		stroke: color-mix(in srgb, var(--rb-knob-warn) 70%, var(--rb-knob-cap-edge, #101318));
		fill: color-mix(in srgb, var(--rb-knob-warn) 18%, var(--rb-knob-cap, #23272f));
	}
	.knob.warn-orange .indicator:not(.white):not(.rainbow) {
		stroke: var(--rb-knob-warn);
	}
	.knob.warn-red .cap {
		stroke: color-mix(in srgb, var(--rb-knob-alarm) 75%, var(--rb-knob-cap-edge, #101318));
		fill: color-mix(in srgb, var(--rb-knob-alarm) 22%, var(--rb-knob-cap, #23272f));
	}
	.knob.warn-red .indicator:not(.white):not(.rainbow) {
		stroke: var(--rb-knob-alarm);
	}
	/* Gothic and Light have no accent glow (transparent), so they draw a
	 * focus outline instead (--rb-focus-outline, theme.css); Default has none. */
	.knob:focus-visible {
		filter: drop-shadow(0 0 3px var(--rb-accent-glow));
		outline: var(--rb-focus-outline, none);
		outline-offset: 1px;
	}
	svg {
		display: block;
	}
	.rb-inert {
		cursor: default;
	}
	/* Selected (shift+click) and linked (alt+click) must read without hovering,
	 * or the DJ cannot tell what the global wheel is about to move. */
	.knob.knob-selected .ring {
		stroke: var(--rb-accent);
		stroke-width: 1.5;
	}
	.knob.knob-linked .ring {
		stroke: var(--rb-knob-warn);
		stroke-dasharray: 3 2;
	}
	/* The knob hooks are Light-only (theme.css); other skins take the fallback. */
	.ring {
		fill: var(--rb-knob-ring, #0b0d10);
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.cap {
		fill: var(--rb-knob-cap, #23272f);
		stroke: var(--rb-knob-cap-edge, #101318);
		stroke-width: 1;
	}
	.indicator {
		stroke: var(--rb-accent);
		stroke-width: 2;
		stroke-linecap: round;
	}
	.indicator.white {
		stroke: var(--rb-knob-indicator-white, #f0f2f5);
	}
	.indicator.rainbow {
		stroke: var(--knob-rainbow-0);
		animation: knob-rainbow 4.5s ease-in-out infinite alternate;
	}
	@keyframes knob-rainbow {
		0% {
			stroke: var(--knob-rainbow-0);
		}
		33% {
			stroke: var(--knob-rainbow-1);
		}
		66% {
			stroke: var(--knob-rainbow-2);
		}
		100% {
			stroke: var(--knob-rainbow-3);
		}
	}
	/* COLORFX-10: the caption is capped to the knob column (dial plus a fixed
	 * overhang, see knob-caption-fit.ts) so it can never reach a neighbour's
	 * caption; text that still does not fit at 7px is cut with an ellipsis. */
	.knob {
		max-width: var(--knob-caption-max);
	}
	.label {
		display: inline-flex;
		align-items: center;
		max-width: var(--knob-caption-max);
		font-size: var(--knob-caption-size);
		letter-spacing: 0.04em;
		color: var(--rb-text-dim);
		line-height: 1;
		white-space: nowrap;
	}
	.label-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.label-lead {
		display: inline-block;
		margin-right: 2px;
		vertical-align: middle;
		line-height: 0;
	}
	.knob.knob-stem-accent .indicator:not(.white):not(.rainbow) {
		stroke: var(--knob-accent-color);
	}
	.knob.knob-stem-accent .cap {
		stroke: color-mix(in srgb, var(--knob-accent-color) 80%, var(--rb-knob-cap-edge, #101318));
		fill: color-mix(in srgb, var(--knob-accent-color) var(--rb-knob-stem-tint, 42%), var(--rb-knob-cap, #23272f));
	}
	.knob.knob-stem-accent .label {
		color: var(--knob-accent-color);
	}
</style>
