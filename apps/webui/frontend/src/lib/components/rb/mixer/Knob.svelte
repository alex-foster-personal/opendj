<script lang="ts">
	/**
	 * Rotary knob: SVG dark ring + indicator line (SCREENSHOT-SPEC 6).
	 * Real knobs: drag-vertical to turn, double-click resets to resetValue
	 * (0.5 for EQ/filter, 0 for CUE<>MASTER mix), arrow keys nudge, mouse
	 * wheel nudges. Inert knobs render identically but
	 * ignore input and carry the standard tooltip.
	 *
	 * All input routes through $lib/rb/knob-control (H5): shift+click selects
	 * a dial so the global scroll wheel keeps nudging it wherever the pointer
	 * goes, alt+click links two dials so one turn moves them inversely with the
	 * rising side over-boosted by KNOB_CFG.linkStagger - which is what stops the
	 * bass dipping through a crossover. Sensitivity lives in KNOB_CFG, not here,
	 * so horizontal drag stays the fine-adjust axis.
	 */
	import { onDestroy } from 'svelte';
	import { createDeferredClickGuard } from '$lib/rb/deferred-click';
	import { plannedTitle } from '$lib/rb/planned-explainers';
	import {
		KNOB_CFG,
		altClickKnob,
		isKnobLinked,
		isKnobSelected,
		linkedPartnerId,
		pointerTravelIsDrag,
		readKnobValue,
		registerKnob,
		setKnobAbsolute,
		setKnobFromDrag,
		setKnobHovered,
		shiftClickKnob,
		unregisterKnob
	} from '$lib/rb/knob-control.svelte';
	import { wheelAdjust } from '$lib/rb/wheel-adjust';
	import { midiTakeoverGhost } from '$lib/rb/midi/takeover-ui.svelte';

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
		onsingleclick
	}: Props = $props();

	const SWEEP_DEG = 270; // -135deg .. +135deg like rekordbox knobs
	const RAINBOW_STOPS = ['#e23a32', '#e8a13a', '#d7d83a', '#35c04f'] as const;
	const visualStyle = $derived.by(() => {
		if (!Number.isFinite(size) || size <= 0) {
			throw new Error(`Knob size must be a positive finite number, got ${size}`);
		}
		return (
			`--knob-dial-size: ${size}px; ` +
			`--knob-caption-size: 8px; ` +
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
	let dragPartnerId: string | null = null;
	let dragStartPartnerValue: number | null = null;
	let dragging = false;
	let pointerMoved = false;
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

	const angleDeg = $derived((value - 0.5) * SWEEP_DEG);
	/** |offset| from center: >0.15 (~30% of half-throw) orange, >0.25 (~50%) red. */
	const warn = $derived.by((): 'none' | 'orange' | 'red' => {
		if (accentColor) return 'none';
		const d = Math.abs(value - 0.5);
		if (d > 0.25) return 'red';
		if (d > 0.15) return 'orange';
		return 'none';
	});

	function handlePointerDown(e: PointerEvent): void {
		// Right and middle clicks belong to the context menu, not the dial.
		if (!live || e.button !== 0) return;
		// Shift = select for the global wheel; Alt = arm/complete a link pair.
		// Neither starts a drag, so a modifier click never also turns the dial.
		if (e.shiftKey) {
			e.preventDefault();
			shiftClickKnob(knobId);
			return;
		}
		if (e.altKey) {
			e.preventDefault();
			altClickKnob(knobId);
			return;
		}
		dragging = true;
		pointerMoved = false;
		singleClickGuard.cancel();
		dragStartX = e.clientX;
		dragStartY = e.clientY;
		dragStartValue = value;
		// Baselines are captured ONCE so the link stagger cannot accumulate
		// across pointermove frames.
		dragPartnerId = linkedPartnerId(knobId);
		dragStartPartnerValue = dragPartnerId === null ? null : readKnobValue(dragPartnerId);
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handlePointerMove(e: PointerEvent): void {
		if (!dragging || !live) return;
		if (!pointerMoved) {
			// Only a knob with a click action has a click to protect: jitter inside the
			// slop stays that click. Every other knob keeps moving from the first pixel.
			const insideSlop = !pointerTravelIsDrag(e.clientX - dragStartX, e.clientY - dragStartY);
			if (onsingleclick !== undefined && insideSlop) return;
			pointerMoved = true;
		}
		const dy = dragStartY - e.clientY; // up = clockwise = increase
		const dx = e.clientX - dragStartX; // right = increase, far less sensitive
		const target =
			dragStartValue + dy / KNOB_CFG.dragVerticalPx + dx / KNOB_CFG.dragHorizontalPx;
		setKnobFromDrag(knobId, dragStartValue, target, dragPartnerId, dragStartPartnerValue);
	}

	function handlePointerUp(e: PointerEvent): void {
		if (!dragging) return;
		const wasDrag = pointerMoved;
		dragging = false;
		dragPartnerId = null;
		dragStartPartnerValue = null;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
		if (!live || wasDrag || onsingleclick === undefined) return;
		singleClickGuard.schedule(() => onsingleclick?.());
	}

	function handleDblClick(): void {
		singleClickGuard.cancel();
		if (!live) return;
		setKnobAbsolute(knobId, resetValue);
	}

	function handleKeyDown(e: KeyboardEvent): void {
		if (!live) return;
		if (e.key === 'ArrowUp' || e.key === 'ArrowRight') {
			e.preventDefault();
			setKnobAbsolute(knobId, value + KNOB_CFG.keyStep);
		} else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') {
			e.preventDefault();
			setKnobAbsolute(knobId, value - KNOB_CFG.keyStep);
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
	aria-valuenow={value}
	aria-disabled={inert}
	tabindex={inert ? -1 : 0}
	style={visualStyle}
	onpointerenter={() => setKnobHovered(live ? knobId : null)}
	onpointerleave={() => setKnobHovered(null)}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	ondblclick={handleDblClick}
	onkeydown={handleKeyDown}
	use:wheelAdjust={{
		step: KNOB_CFG.scrollStep,
		get: () => value,
		// Through the registry, so a linked partner moves with it.
		set: (next) => setKnobAbsolute(knobId, next),
		disabled: !live
	}}
	title={inert
		? `${label}: ${plannedTitle('mixer-knob')}`
		: `${label}${selected ? ' - selected: the scroll wheel nudges this dial from anywhere' : ''}${linked ? ' - linked: turning this dial moves its partner the other way' : ''}`}
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
	{#if takeoverGhost !== null}
		<span class="sr-only">Hardware at {(takeoverGhost.value * 100).toFixed(0)}%; move to {(takeoverGhost.target * 100).toFixed(0)}% to pick up.</span>
	{/if}
	<span class="label">{label}</span>
</div>

<style>
	.knob {
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
		stroke: #f2b84b;
		stroke-width: 1.5;
		stroke-dasharray: 1.5 1;
	}
	.takeover-armed .ring { stroke: #f2b84b; }
	.knob.warn-orange .cap {
		stroke: color-mix(in srgb, var(--rb-knob-warn) 70%, #101318);
		fill: color-mix(in srgb, var(--rb-knob-warn) 18%, #23272f);
	}
	.knob.warn-orange .indicator:not(.white):not(.rainbow) {
		stroke: var(--rb-knob-warn);
	}
	.knob.warn-red .cap {
		stroke: color-mix(in srgb, var(--rb-knob-alarm) 75%, #101318);
		fill: color-mix(in srgb, var(--rb-knob-alarm) 22%, #23272f);
	}
	.knob.warn-red .indicator:not(.white):not(.rainbow) {
		stroke: var(--rb-knob-alarm);
	}
	.knob:focus-visible {
		filter: drop-shadow(0 0 3px var(--rb-accent-glow));
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
	.ring {
		fill: #0b0d10;
		stroke: var(--rb-border);
		stroke-width: 1;
	}
	.cap {
		fill: #23272f;
		stroke: #101318;
		stroke-width: 1;
	}
	.indicator {
		stroke: var(--rb-accent);
		stroke-width: 2;
		stroke-linecap: round;
	}
	.indicator.white {
		stroke: #f0f2f5;
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
	.label {
		font-size: var(--knob-caption-size);
		letter-spacing: 0.04em;
		color: var(--rb-text-dim);
		line-height: 1;
	}
	.knob.knob-stem-accent .indicator:not(.white):not(.rainbow) {
		stroke: var(--knob-accent-color);
	}
	.knob.knob-stem-accent .cap {
		stroke: color-mix(in srgb, var(--knob-accent-color) 80%, #101318);
		fill: color-mix(in srgb, var(--knob-accent-color) 42%, #23272f);
	}
	.knob.knob-stem-accent .label {
		color: var(--knob-accent-color);
	}
</style>
