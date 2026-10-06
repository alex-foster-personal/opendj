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
/**
 * `draw()`'s repaint-skip bookkeeping (was two component-local `let`s in
 * `WaveRow.svelte`). Plain object, not `$state` -- nothing here is read by
 * Svelte's reactivity, only by `draw()` itself on the next rAF tick.
 */
export interface PaintScheduleState {
	lastScrollPx: number | null;
	lastVisualInputs: readonly unknown[] | null;
}

export function initPaintScheduleState(): PaintScheduleState {
	return { lastScrollPx: null, lastVisualInputs: null };
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
 * Whether `draw()` should skip repainting this frame: none of the waveform's
 * own visual inputs changed AND the scroll position moved by under a pixel,
 * so the canvas would be redrawn pixel-identical. `force` always repaints
 * (load/seek/resize/anlz-arrival). When the frame is NOT skipped, `state` is
 * updated in place to this frame's inputs -- the same "record the baseline
 * only when we actually painted" bookkeeping the inline version did, just
 * owned here instead of duplicated at each call site.
 */
export function shouldSkipRepaint(
	state: PaintScheduleState,
	force: boolean,
	visualInputs: readonly unknown[],
	scrollPx: number
): boolean {
	const visualsChanged =
		state.lastVisualInputs === null ||
		visualInputs.some((value, index) => !Object.is(value, state.lastVisualInputs?.[index]));
	const skip =
		!force && !visualsChanged && state.lastScrollPx !== null && Math.abs(scrollPx - state.lastScrollPx) < 1;
	if (!skip) {
		state.lastScrollPx = scrollPx;
		state.lastVisualInputs = visualInputs;
	}
	return skip;
}
