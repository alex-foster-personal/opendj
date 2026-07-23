<script lang="ts">
	/**
	 * Rotary knob: SVG dark ring + indicator line (SCREENSHOT-SPEC 6).
	 * Real knobs: drag-vertical to turn, double-click resets to 0.5, arrow
	 * keys nudge. Inert knobs render identically but ignore input and carry
	 * the standard tooltip.
	 */
	interface Props {
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
	}

	let { label, value, onchange, inert = false, tone = 'accent' }: Props = $props();

	const INERT_TITLE = 'not implemented - see PARITY-TODO';
	const SWEEP_DEG = 270; // -135deg .. +135deg like rekordbox knobs
	const DRAG_RANGE_PX = 150; // full 0..1 sweep over 150px of vertical drag
	const KEY_STEP = 0.02;
	const SIZE = 30;

	let dragStartY = 0;
	let dragStartValue = 0;
	let dragging = false;

	const angleDeg = $derived((value - 0.5) * SWEEP_DEG);
	/** |offset| from center: >0.15 (~30% of half-throw) orange, >0.25 (~50%) red. */
	const warn = $derived.by((): 'none' | 'orange' | 'red' => {
		const d = Math.abs(value - 0.5);
		if (d > 0.25) return 'red';
		if (d > 0.15) return 'orange';
		return 'none';
	});

	function _clamp01(v: number): number {
		return Math.min(1, Math.max(0, v));
	}

	function handlePointerDown(e: PointerEvent): void {
		if (inert || !onchange) return;
		dragging = true;
		dragStartY = e.clientY;
		dragStartValue = value;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handlePointerMove(e: PointerEvent): void {
		if (!dragging || !onchange) return;
		const dy = dragStartY - e.clientY; // up = clockwise = increase
		onchange(_clamp01(dragStartValue + dy / DRAG_RANGE_PX));
	}

	function handlePointerUp(e: PointerEvent): void {
		if (!dragging) return;
		dragging = false;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
	}

	function handleDblClick(): void {
		if (inert || !onchange) return;
		onchange(0.5); // center detent reset
	}

	function handleKeyDown(e: KeyboardEvent): void {
		if (inert || !onchange) return;
		if (e.key === 'ArrowUp' || e.key === 'ArrowRight') {
			e.preventDefault();
			onchange(_clamp01(value + KEY_STEP));
		} else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') {
			e.preventDefault();
			onchange(_clamp01(value - KEY_STEP));
		}
	}
</script>

<div class="knob" class:rb-inert={inert} class:warn-orange={warn === 'orange'} class:warn-red={warn === 'red'} title={inert ? INERT_TITLE : label}>
	<svg
		width={SIZE}
		height={SIZE}
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
