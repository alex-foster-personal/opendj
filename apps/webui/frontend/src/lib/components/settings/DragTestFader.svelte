<script lang="ts">
	/**
	 * R6 test fader: the channel fader's drag path (shared R5 gain and falloff,
	 * group select, the dragged control leads, Opt flip) with none of its deck
	 * wiring. Wired to nothing: no audio, no commands, just local state.
	 */
	import {
		beginSelectionDrag,
		isKnobSelected,
		registerKnob,
		setKnobHovered,
		shiftClickKnob,
		stepSelectionDrag,
		unregisterKnob,
		type SelectionDragStart
	} from '$lib/rb/knob-control.svelte';
	import { createDragTracker, trackDragMove, type DragTracker } from '$lib/rb/drag-sensitivity.svelte';

	interface Props {
		controlId: string;
		label: string;
		value: number;
		onchange: (value: number) => void;
	}

	let { controlId, label, value, onchange }: Props = $props();

	const TRACK_PX = 96;
	let tracker: DragTracker | null = null;
	let group: SelectionDragStart | null = null;
	let virtual = 0;
	const selected = $derived(isKnobSelected(controlId));

	$effect(() => {
		const id = controlId;
		registerKnob({ id, getValue: () => value, setValue: (next) => onchange(next) });
		return () => unregisterKnob(id);
	});

	function handlePointerDown(e: PointerEvent): void {
		if (e.button !== 0) return;
		if (e.shiftKey) {
			e.preventDefault();
			shiftClickKnob(controlId);
			return;
		}
		tracker = createDragTracker(e.clientX, e.clientY);
		group = beginSelectionDrag(controlId);
		virtual = value;
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
	}

	function handlePointerMove(e: PointerEvent): void {
		if (tracker === null) return;
		const delta = trackDragMove(tracker, e.clientX, e.clientY).dy / TRACK_PX;
		if (group !== null) {
			stepSelectionDrag(group, delta, e.altKey);
			return;
		}
		virtual += delta;
		onchange(Math.min(1, Math.max(0, virtual)));
	}

	function handlePointerUp(e: PointerEvent): void {
		if (tracker === null) return;
		tracker = null;
		group = null;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
	}
</script>

<div
	class="dtf"
	class:selected
	role="slider"
	tabindex="-1"
	aria-label={label}
	aria-valuemin={0}
	aria-valuemax={1}
	aria-valuenow={value}
	data-knob-id={controlId}
	data-testid={`drag-test-${controlId}`}
	style={`height: ${TRACK_PX + 12}px;`}
	onpointerdown={handlePointerDown}
	onpointermove={handlePointerMove}
	onpointerup={handlePointerUp}
	onpointercancel={handlePointerUp}
	onpointerenter={() => setKnobHovered(controlId)}
	onpointerleave={() => setKnobHovered(null)}
>
	<div class="dtf-track"></div>
	<div class="dtf-thumb" style={`top: ${(1 - value) * TRACK_PX}px;`}></div>
</div>

<style>
	.dtf {
		position: relative;
		width: 18px;
		touch-action: none;
		cursor: ns-resize;
	}
	.dtf-track {
		position: absolute;
		left: 8px;
		top: 6px;
		bottom: 6px;
		width: 2px;
		background: var(--rb-border, #333);
	}
	.dtf-thumb {
		position: absolute;
		left: 1px;
		width: 16px;
		height: 12px;
		border-radius: 2px;
		background: var(--rb-text-dim, #9aa4b2);
	}
	.dtf.selected .dtf-thumb {
		box-shadow: 0 0 0 1px var(--rb-accent, #4aa3ff);
	}
</style>
