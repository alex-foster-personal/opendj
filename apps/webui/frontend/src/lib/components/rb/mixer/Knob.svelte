<script lang="ts">
	/**
	 * Rotary knob: SVG dark ring + blue indicator line (SCREENSHOT-SPEC 6).
	 * Real knobs: drag-vertical to turn, double-click resets to 0.5, arrow
	 * keys nudge. Inert knobs render identically but ignore input and carry
	 * the standard tooltip.
	 */
	interface Props {
		/** Small caps label under the knob (TRIM / HIGH / MID / LOW / MIX ...). */
		label: string;
		/** Position 0..1; 0.5 = center detent (unity / flat). */
		value: number;
		/** Called with the new 0..1 value on every drag/keyboard change. */
		onchange?: (value: number) => void;
		/** True = visually authentic but ignores all input. */
		inert?: boolean;
	}

	let { label, value, onchange, inert = false }: Props = $props();

	const INERT_TITLE = 'not implemented - see PARITY-TODO';
	const SWEEP_DEG = 270; // -135deg .. +135deg like rekordbox knobs
	const DRAG_RANGE_PX = 150; // full 0..1 sweep over 150px of vertical drag
	const KEY_STEP = 0.02;

	let dragStartY = 0;
	let dragStartValue = 0;
	let dragging = false;

	const angleDeg = $derived((value - 0.5) * SWEEP_DEG);

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

<div class="knob" class:rb-inert={inert} title={inert ? INERT_TITLE : label}>
	<svg
		width="26"
		height="26"
		viewBox="0 0 26 26"
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
		<!-- outer ring -->
		<circle cx="13" cy="13" r="12" class="ring" />
		<!-- knob cap -->
		<circle cx="13" cy="13" r="9.5" class="cap" />
		<!-- blue indicator line, rotated around center -->
		<g style={`transform: rotate(${angleDeg}deg); transform-origin: 13px 13px;`}>
			<line x1="13" y1="4.5" x2="13" y2="10.5" class="indicator" />
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
	.label {
		font-size: 8px;
		letter-spacing: 0.04em;
		color: var(--rb-text-dim);
		line-height: 1;
	}
</style>
