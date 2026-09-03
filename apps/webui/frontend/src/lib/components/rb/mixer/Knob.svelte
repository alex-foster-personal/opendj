<script lang="ts">
	/**
	 * Rotary knob: SVG dark ring + indicator line (SCREENSHOT-SPEC 6).
	 * Real knobs: drag-vertical to turn, double-click resets to 0.5, arrow
	 * keys nudge, mouse wheel nudges. Inert knobs render identically but
	 * ignore input and carry the standard tooltip.
	 *
	 * All input routes through $lib/rb/knob-control (H5): shift+click selects
	 * a dial so the global scroll wheel keeps nudging it wherever the pointer
	 * goes, alt+click links two dials so one turn moves them inversely with the
	 * rising side over-boosted by KNOB_CFG.linkStagger - which is what stops the
	 * bass dipping through a crossover. Sensitivity lives in KNOB_CFG, not here,
	 * so horizontal drag stays the fine-adjust axis.
	 */
	import {
		KNOB_CFG,
		altClickKnob,
		isKnobLinked,
		isKnobSelected,
		linkedPartnerId,
		readKnobValue,
		registerKnob,
		setKnobAbsolute,
		setKnobFromDrag,
		setKnobHovered,
		shiftClickKnob,
		unregisterKnob
	} from '$lib/rb/knob-control.svelte';
	import { wheelAdjust } from '$lib/rb/wheel-adjust';

	interface Props {
		/** Stable control id from knobId(deckId, role) - the knob-control registry key. */
		knobId: string;
		/** Small caps label under the knob (TRIM / HI / MID / LOW / FILTER ...). */
		label: string;
		/** Position 0..1; 0.5 = center detent (unity / flat). */
		value: number;
		/** Called with the new 0..1 value on every drag/keyboard change. */
		onchange?: (value: number) => void;
		/** True = visually authentic but ignores all input. */
		inert?: boolean;
		/** Indicator stroke tone. */
		tone?: 'accent' | 'white' | 'rainbow';
		/** Rendered diameter in px. viewBox stays fixed at 30x30, so this scales the whole ring/cap/indicator (and hit area, since the svg IS the hit area) uniformly. */
		size?: number;
	}

	let { knobId, label, value, onchange, inert = false, tone = 'accent', size = 30 }: Props =
		$props();

	const INERT_TITLE = 'not implemented - see PARITY-TODO';
	const SWEEP_DEG = 270; // -135deg .. +135deg like rekordbox knobs

	const live = $derived(!inert && onchange !== undefined);

	let dragStartX = 0;
	let dragStartY = 0;
	let dragStartValue = 0;
	let dragPartnerId: string | null = null;
	let dragStartPartnerValue: number | null = null;
	let dragging = false;

	$effect(() => {
		if (!live) return;
		registerKnob({ id: knobId, getValue: () => value, setValue: (next) => onchange?.(next) });
		return () => unregisterKnob(knobId);
	});

	const selected = $derived(live && isKnobSelected(knobId));
	const linked = $derived(live && isKnobLinked(knobId));

	const angleDeg = $derived((value - 0.5) * SWEEP_DEG);
	/** |offset| from center: >0.15 (~30% of half-throw) orange, >0.25 (~50%) red. */
	const warn = $derived.by((): 'none' | 'orange' | 'red' => {
		const d = Math.abs(value - 0.5);
		if (d > 0.25) return 'red';
		if (d > 0.15) return 'orange';
		return 'none';
	});

	function handlePointerDown(e: PointerEvent): void {
		if (!live) return;
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
		const dy = dragStartY - e.clientY; // up = clockwise = increase
		const dx = e.clientX - dragStartX; // right = increase, far less sensitive
		const target =
			dragStartValue + dy / KNOB_CFG.dragVerticalPx + dx / KNOB_CFG.dragHorizontalPx;
		setKnobFromDrag(knobId, dragStartValue, target, dragPartnerId, dragStartPartnerValue);
	}

	function handlePointerUp(e: PointerEvent): void {
		if (!dragging) return;
		dragging = false;
		dragPartnerId = null;
		dragStartPartnerValue = null;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
	}

	function handleDblClick(): void {
		if (!live) return;
		setKnobAbsolute(knobId, 0.5); // center detent reset
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
	data-knob-id={knobId}
	onpointerenter={() => setKnobHovered(live ? knobId : null)}
	onpointerleave={() => setKnobHovered(null)}
	title={inert
		? INERT_TITLE
		: `${label}${selected ? ' - selected: the scroll wheel nudges this dial from anywhere' : ''}${linked ? ' - linked: turning this dial moves its partner the other way' : ''}`}
>
	<svg
		width={size}
		height={size}
		viewBox="0 0 30 30"
		role="slider"
		aria-label={label}
		aria-valuemin={0}
		aria-valuemax={1}
		aria-valuenow={value}
		aria-disabled={inert}
		tabindex={inert ? -1 : 0}
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
	</svg>
	<span class="label">{label}</span>
</div>

<style>
	.knob {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 1px;
	}
	.knob.warn-orange .cap {
		stroke: color-mix(in srgb, var(--rb-orange) 70%, #101318);
		fill: color-mix(in srgb, var(--rb-orange) 18%, #23272f);
	}
	.knob.warn-orange .indicator:not(.white):not(.rainbow) {
		stroke: var(--rb-orange);
	}
	.knob.warn-red .cap {
		stroke: color-mix(in srgb, var(--rb-red) 75%, #101318);
		fill: color-mix(in srgb, var(--rb-red) 22%, #23272f);
	}
	.knob.warn-red .indicator:not(.white):not(.rainbow) {
		stroke: var(--rb-red);
	}
	svg {
		cursor: ns-resize;
		touch-action: none;
		outline: none;
		display: block;
	}
	svg:focus-visible {
		filter: drop-shadow(0 0 3px var(--rb-accent-glow));
	}
	.rb-inert svg {
		cursor: default;
	}
	/* Selected (shift+click) and linked (alt+click) must read without hovering,
	 * or the DJ cannot tell what the global wheel is about to move. */
	.knob.knob-selected .ring {
		stroke: var(--rb-accent);
		stroke-width: 1.5;
	}
	.knob.knob-linked .ring {
		stroke: var(--rb-orange);
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
		stroke: #e23a32;
		animation: knob-rainbow 4.5s linear infinite;
	}
	@keyframes knob-rainbow {
		0% {
			stroke: #e23a32;
		}
		16% {
			stroke: #e8a13a;
		}
		33% {
			stroke: #35c04f;
		}
		50% {
			stroke: #2f6fd6;
		}
		66% {
			stroke: #7b5cff;
		}
		83% {
			stroke: #d0348a;
		}
		100% {
			stroke: #e23a32;
		}
	}
	.label {
		font-size: 8px;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim);
		line-height: 1;
	}
</style>
