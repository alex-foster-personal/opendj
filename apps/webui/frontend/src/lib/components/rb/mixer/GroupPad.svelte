<script lang="ts">
	/**
	 * R7 (the maintainer, Wed 7 Oct 2026): "when several are selected a two-axis modal
	 * appears in top right (with X / Esc to close) with a dot to click-drag in
	 * the middle (circle)".
	 *
	 * A relative nudge pad: X moves channel A's selected items, Y channel B's
	 * (one-channel group: the first item on X, the rest on Y), through the same
	 * R5 gain and distance falloff and the same typed command path as a drag.
	 * The dot springs back to centre on release; values stay where they were
	 * left. X or Esc closes it and clears the selection. It never takes focus,
	 * so performance shortcuts keep working; Esc is handled while the pointer
	 * is over it, and the global Esc already clears the selection anyway.
	 */
	import {
		KNOB_CFG,
		beginPadDrag,
		clearKnobSelection,
		knobGroupOf,
		knobUi,
		padAxes,
		stepPadDrag,
		type SelectionDragStart
	} from '$lib/rb/knob-control.svelte';
	import {
		createDragTracker,
		trackDragMove,
		type DragTracker
	} from '$lib/rb/drag-sensitivity.svelte';
	import { isSettingsOpen } from '$lib/settings/overlay.svelte';
	import { selectorNode } from '$lib/keyboard/selector-register';
	import { groupPadSelectorId } from './mixer-extra-selector-ids';

	interface Props {
		/**
		 * True for the copy SettingsOverlay mounts above its backdrop. While
		 * settings is open only that copy shows, so a group of the drag
		 * tester's controls (or of mixer dials) always has a usable pad, on
		 * routes with no mixer too; the Mixer's copy shows the rest of the
		 * time (Sol P1, PR #6829).
		 */
		inSettings?: boolean;
	}

	let { inSettings = false }: Props = $props();

	/** SVG units: the circle radius in a -100..100 viewBox. */
	const R = 90;
	const TICKS = [0.25, 0.5, 0.75, 1] as const;
	const MAIN_DEG = [0, 90];
	const DIAG_DEG = [45, 135];
	const HALF_DEG = [22.5, 67.5, 112.5, 157.5];

	// The settings copy sits outside the /performance selector tree, so only the Mixer's copy is a node.
	const selectorEnabled = $derived(!inSettings);
	const open = $derived(knobUi.selectedIds.length >= 2 && isSettingsOpen() === inSettings);
	const axes = $derived(padAxes(knobUi.selectedIds));
	const xLabel = $derived(_axisLabel(axes.x));
	const yLabel = $derived(_axisLabel(axes.y));

	let svgEl: SVGSVGElement | undefined = $state();
	let dot = $state({ x: 0, y: 0 });
	let tracker: DragTracker | null = null;
	let session: SelectionDragStart | null = null;
	let pressX = 0;
	let pressY = 0;
	let pointerOver = false;

	function _axisLabel(ids: readonly string[]): string {
		if (ids.length === 0) return '';
		const decks = [...new Set(ids.map(knobGroupOf))];
		return `${decks.map((d) => `deck ${d}`).join(', ')} (${ids.length})`;
	}

	function _line(deg: number): { x1: number; y1: number; x2: number; y2: number } {
		const rad = (deg * Math.PI) / 180;
		const x = Math.cos(rad) * R;
		const y = Math.sin(rad) * R;
		return { x1: -x, y1: y, x2: x, y2: -y };
	}

	function _ticks(deg: number): Array<{ x1: number; y1: number; x2: number; y2: number }> {
		const rad = (deg * Math.PI) / 180;
		const ux = Math.cos(rad);
		const uy = -Math.sin(rad);
		const half = 4;
		const out = [];
		for (const f of TICKS) {
			for (const sign of [1, -1]) {
				const cx = ux * R * f * sign;
				const cy = uy * R * f * sign;
				out.push({
					x1: cx - uy * half,
					y1: cy + ux * half,
					x2: cx + uy * half,
					y2: cy - ux * half
				});
			}
		}
		return out;
	}

	function close(): void {
		clearKnobSelection();
	}

	function handlePointerDown(e: PointerEvent): void {
		if (e.button !== 0) return;
		// Never take focus from the performance shortcuts.
		e.preventDefault();
		session = beginPadDrag();
		if (session === null) return;
		tracker = createDragTracker(e.clientX, e.clientY);
		pressX = e.clientX;
		pressY = e.clientY;
		(e.currentTarget as Element).setPointerCapture(e.pointerId);
	}

	function handlePointerMove(e: PointerEvent): void {
		if (tracker === null || session === null) return;
		const step = trackDragMove(tracker, e.clientX, e.clientY);
		stepPadDrag(session, step.dx / KNOB_CFG.dragVerticalPx, step.dy / KNOB_CFG.dragVerticalPx);
		// The dot follows the pointer, clamped to the circle (screen px -> SVG units).
		const scale = svgEl ? 200 / svgEl.getBoundingClientRect().width : 1;
		let x = (e.clientX - pressX) * scale;
		let y = (e.clientY - pressY) * scale;
		const len = Math.hypot(x, y);
		if (len > R) {
			x = (x / len) * R;
			y = (y / len) * R;
		}
		dot = { x, y };
	}

	/** Ends the drag: the next pointermove does nothing until a new press. */
	function endDrag(): void {
		tracker = null;
		session = null;
		dot = { x: 0, y: 0 };
	}

	function handlePointerUp(e: PointerEvent): void {
		if (tracker === null) return;
		endDrag();
		(e.currentTarget as Element).releasePointerCapture(e.pointerId);
	}

	// Esc or X mid-drag closes the pad before any pointerup reaches it, and this
	// copy stays mounted, so without this a later move over the reopened pad
	// would resume the old session on the old group's controls.
	$effect(() => {
		if (!open) endDrag();
	});

	$effect(() => {
		if (!open) return;
		const onKey = (e: KeyboardEvent): void => {
			if (e.key === 'Escape' && pointerOver) close();
		};
		window.addEventListener('keydown', onKey);
		return () => window.removeEventListener('keydown', onKey);
	});
</script>

{#if open}
	<div
		class="group-pad"
		role="group"
		aria-label="Group pad"
		data-testid="mixer-group-pad"
		data-knob-id="group-pad"
		use:selectorNode={{ id: groupPadSelectorId(), kind: 'leaf', enabled: selectorEnabled }}
		onpointerenter={() => (pointerOver = true)}
		onpointerleave={() => (pointerOver = false)}
	>
		<div class="gp-head">
			<span class="gp-title">Group</span>
			<button
				type="button"
				class="gp-close"
				tabindex="-1"
				aria-label="Close the group pad and clear the selection"
				title="Close (Esc) and clear the selection"
				data-testid="mixer-group-pad-close"
				onpointerdown={(e) => e.preventDefault()}
				onclick={close}>X</button
			>
		</div>
		<svg
			bind:this={svgEl}
			class="gp-svg"
			viewBox="-100 -100 200 200"
			data-testid="mixer-group-pad-surface"
			role="slider"
			aria-label="Two-axis group nudge"
			aria-valuenow={0}
			tabindex="-1"
			onpointerdown={handlePointerDown}
			onpointermove={handlePointerMove}
			onpointerup={handlePointerUp}
			onpointercancel={handlePointerUp}
			onlostpointercapture={endDrag}
		>
			<circle r={R} class="gp-ring" />
			{#each HALF_DEG as deg (deg)}
				{@const l = _line(deg)}
				<line {...l} class="gp-half" />
				{#each _ticks(deg) as t, i (i)}<line {...t} class="gp-tick gp-tick-half" />{/each}
			{/each}
			{#each DIAG_DEG as deg (deg)}
				{@const l = _line(deg)}
				<line {...l} class="gp-diag" />
				{#each _ticks(deg) as t, i (i)}<line {...t} class="gp-tick" />{/each}
			{/each}
			{#each MAIN_DEG as deg (deg)}
				{@const l = _line(deg)}
				<line {...l} class="gp-axis" />
				{#each _ticks(deg) as t, i (i)}<line {...t} class="gp-tick" />{/each}
			{/each}
			<circle cx={dot.x} cy={dot.y} r="9" class="gp-dot" data-testid="mixer-group-pad-dot" />
		</svg>
		<div class="gp-legend">
			<span>X: {xLabel}</span>
			<span>Y: {yLabel}</span>
		</div>
	</div>
{/if}

<style>
	.group-pad {
		position: fixed;
		top: 48px;
		right: 12px;
		z-index: 70;
		width: 168px;
		padding: 6px;
		background: var(--rb-panel-raised, #1d2129);
		border: 1px solid var(--rb-border, #333);
		border-radius: 6px;
		box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);
		color: var(--rb-text, #e6e9ef);
		font-size: 10px;
		user-select: none;
	}
	.gp-head {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: 4px;
	}
	.gp-close {
		border: 1px solid var(--rb-border, #333);
		background: transparent;
		color: inherit;
		font-size: 10px;
		line-height: 1;
		padding: 2px 5px;
		cursor: pointer;
	}
	.gp-svg {
		display: block;
		width: 156px;
		height: 156px;
		touch-action: none;
		cursor: grab;
	}
	.gp-ring {
		fill: none;
		stroke: var(--rb-border, #555);
		stroke-width: 1.5;
	}
	.gp-axis {
		stroke: var(--rb-text, #e6e9ef);
		stroke-width: 3;
	}
	.gp-diag {
		stroke: var(--rb-text, #e6e9ef);
		stroke-width: 1.2;
		opacity: 0.75;
	}
	.gp-half {
		stroke: var(--rb-text-dim, #9aa4b2);
		stroke-width: 0.7;
		stroke-dasharray: 2 3;
		opacity: 0.45;
	}
	.gp-tick {
		stroke: var(--rb-text, #e6e9ef);
		stroke-width: 1;
		opacity: 0.7;
	}
	.gp-tick-half {
		opacity: 0.35;
	}
	.gp-dot {
		fill: var(--rb-accent, #4aa3ff);
		stroke: #fff;
		stroke-width: 1.5;
	}
	.gp-legend {
		display: flex;
		justify-content: space-between;
		margin-top: 4px;
		opacity: 0.8;
	}
</style>
