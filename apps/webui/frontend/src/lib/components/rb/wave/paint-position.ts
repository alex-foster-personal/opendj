/**
 * Waveform paint scheduling: the scroll offset for a paint position, and when a
 * frame can skip repainting.
 *
 * The paint POSITION itself is no longer projected here. Pin 53ba89ca8ddc
 * measured why the raw sample cannot be painted directly (the output clock
 * advances in device-buffer chunks, so the raw value holds for a frame or two
 * and then jumps), and Mon 5 Oct 2026 found that projecting from the frame that
 * noticed a sample painted synced decks a frame apart. Both are answered by the
 * one per-deck playhead clock every animated element now shares
 * (`$lib/rb/playhead-clock.ts`, ANIM-CLOCK-01).
 */
/** Must match the `writeVisualInputs` argument list in `WaveRow.svelte` `draw()`. */
export const VISUAL_INPUT_COUNT = 33;

function visualInputSlot(): unknown[] {
	return Array.from({ length: VISUAL_INPUT_COUNT }, () => undefined);
}

/**
 * `draw()`'s repaint-skip bookkeeping (was two component-local `let`s in
 * `WaveRow.svelte`). Plain object, not `$state` -- nothing here is read by
 * Svelte's reactivity, only by `draw()` itself on the next rAF tick.
 */
export interface PaintScheduleState {
	lastScrollPx: number | null;
	/** This frame's inputs, filled in place by `writeVisualInputs`. */
	inputs: unknown[];
	/** The inputs of the last frame that actually painted. */
	lastInputs: unknown[];
}

export function initPaintScheduleState(): PaintScheduleState {
	return { lastScrollPx: null, inputs: visualInputSlot(), lastInputs: visualInputSlot() };
}

/** The horizontal scroll offset (px) for the waveform window at this paint position. */
export function paintScrollPx(
	paintPositionMs: number,
	durationMs: number | null,
	cssW: number,
	windowS: number,
	pitch: number
): number {
	return (durationMs === null ? 0 : paintPositionMs / 1000) * (cssW / (windowS * pitch));
}

/**
 * Fills `state.inputs` in place for this frame. Positional parameters only: a
 * rest parameter would allocate an array every call, which this path avoids.
 */
export function writeVisualInputs(
	state: PaintScheduleState,
	v0: unknown,
	v1: unknown,
	v2: unknown,
	v3: unknown,
	v4: unknown,
	v5: unknown,
	v6: unknown,
	v7: unknown,
	v8: unknown,
	v9: unknown,
	v10: unknown,
	v11: unknown,
	v12: unknown,
	v13: unknown,
	v14: unknown,
	v15: unknown,
	v16: unknown,
	v17: unknown,
	v18: unknown,
	v19: unknown,
	v20: unknown,
	v21: unknown,
	v22: unknown,
	v23: unknown,
	v24: unknown,
	v25: unknown,
	v26: unknown,
	v27: unknown,
	v28: unknown,
	v29: unknown,
	v30: unknown,
	v31: unknown,
	v32: unknown
): void {
	const count = arguments.length - 1;
	if (count !== VISUAL_INPUT_COUNT) {
		throw new Error(
			`writeVisualInputs: got ${count} visual inputs, expected ${VISUAL_INPUT_COUNT}`
		);
	}
	const slot = state.inputs;
	slot[0] = v0;
	slot[1] = v1;
	slot[2] = v2;
	slot[3] = v3;
	slot[4] = v4;
	slot[5] = v5;
	slot[6] = v6;
	slot[7] = v7;
	slot[8] = v8;
	slot[9] = v9;
	slot[10] = v10;
	slot[11] = v11;
	slot[12] = v12;
	slot[13] = v13;
	slot[14] = v14;
	slot[15] = v15;
	slot[16] = v16;
	slot[17] = v17;
	slot[18] = v18;
	slot[19] = v19;
	slot[20] = v20;
	slot[21] = v21;
	slot[22] = v22;
	slot[23] = v23;
	slot[24] = v24;
	slot[25] = v25;
	slot[26] = v26;
	slot[27] = v27;
	slot[28] = v28;
	slot[29] = v29;
	slot[30] = v30;
	slot[31] = v31;
	slot[32] = v32;
}

/**
 * Whether `draw()` should skip repainting this frame: none of the waveform's
 * own visual inputs changed AND the scroll position moved by under a pixel,
 * so the canvas would be redrawn pixel-identical. `force` always repaints
 * (load/seek/resize/anlz-arrival). Inputs come from `state.inputs`, which
 * `writeVisualInputs` filled for this frame. On a repaint the two slots swap;
 * nothing is allocated. When the frame is NOT skipped, `state` is updated in
 * place -- the same "record the baseline only when we actually painted"
 * bookkeeping the inline version did.
 */
export function shouldSkipRepaint(
	state: PaintScheduleState,
	force: boolean,
	scrollPx: number
): boolean {
	const { inputs, lastInputs } = state;
	if (inputs.length !== VISUAL_INPUT_COUNT || lastInputs.length !== VISUAL_INPUT_COUNT) {
		throw new Error(
			`shouldSkipRepaint: visual input slots hold ${inputs.length} and ${lastInputs.length}, expected ${VISUAL_INPUT_COUNT}`
		);
	}
	let visualsChanged = false;
	for (let i = 0; i < VISUAL_INPUT_COUNT; i++) {
		if (!Object.is(inputs[i], lastInputs[i])) {
			visualsChanged = true;
			break;
		}
	}
	const skip =
		!force &&
		!visualsChanged &&
		state.lastScrollPx !== null &&
		Math.abs(scrollPx - state.lastScrollPx) < 1;
	if (!skip) {
		state.lastScrollPx = scrollPx;
		const nextWrite = lastInputs;
		state.inputs = nextWrite;
		state.lastInputs = inputs;
	}
	return skip;
}
