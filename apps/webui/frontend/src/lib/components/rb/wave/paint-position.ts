/**
 * Pin 53ba89ca8ddc: the waveform playhead's paint-time position, projected
 * between the raw transport samples `_tick()` publishes rather than painted
 * straight off them.
 *
 * Measured (headless Chromium, 4s @ 60Hz rAF, a live `AudioContext` with a
 * silent oscillator feeding the destination -- the same shape `_tick()`
 * reads): `getOutputTimestamp().contextTime`, the ground truth
 * `deck.position_ms` is derived from, does NOT advance once per rAF frame.
 * Median per-frame delta was 0.0ms (over half of sampled frames saw no
 * advance at all), with the value then jumping ~17ms in a single frame
 * (p95 17.4ms) to catch up. rAF's own frame cadence was steady across the
 * same run (mean 16.65ms, stdev 1.19ms). Painting the raw value every frame
 * therefore holds the playhead still for a frame or two and then snaps it
 * roughly two frames' worth of pixels forward -- the reported jitter is a
 * sampling-cadence artifact, not a rendering bug and not real motion.
 *
 * A fixed-time smoothing constant (the pin's own "maybe 20ms?" suggestion)
 * was rejected: a low-pass blend lags behind and blurs a real seek, a tempo
 * change, or the presentation clock actually stalling (the sibling
 * `playheadFrozen` detector in `WaveRow.svelte` exists for exactly that
 * history -- Wed 2 Sep 2026's 20-minute silent freeze traced to a stale
 * `outputLatency` reading nobody re-checked). Blurring that signal to fix a
 * cosmetic stutter would trade a real bug class for a cosmetic one.
 *
 * What this does instead is exact interpolation, not smoothing: anchor to
 * the newest raw sample the instant it changes, then project forward at
 * `rate` (the same wall-clock-to-track-time multiplier already used for
 * `scrollPx` in `WaveRow.svelte`, i.e. `deck.pitch`) using the caller's own
 * clock reading. Every fresh raw sample re-anchors immediately, so no drift
 * accumulates and nothing is blurred -- between samples this paints the SAME
 * known constant-rate motion at a finer time resolution, it does not invent
 * one. The caller (`WaveRow.svelte`) falls back to the raw value with
 * `playing=false` or `trusted=false` whenever the deck is not playing or the
 * presentation clock is not trusted, so a genuine stall is exactly as
 * visible as before: the freeze detector folds `deck.position_ms` directly,
 * never this projected value.
 *
 * Anchor = the engine's sample time, not the frame that noticed it (Mon 5 Oct
 * 2026, "waveforms slip apart even though they are syncing"). Each WaveRow
 * runs its own rAF loop, and the engine's `_tick` is another: a row whose
 * callback runs BEFORE the tick sees each sample one frame late, and anchoring
 * there projects it forward from the wrong instant. Two synced decks then
 * painted a frame apart (measured on demon-llama at a busy page's ~7 fps: 80 to
 * 190 ms of beat phase on screen while the engine held them 1 ms apart). The
 * engine now records when it sampled each position (`notePositionSample`), and
 * a row projects from that instant, so every row paints every deck from the
 * same clock. `sampledAtMs` null (a position written outside the per-frame
 * publish, e.g. a seek) keeps the notice-time anchor.
 */

export interface PositionInterpolatorState {
	lastRawPositionMs: number | null;
	lastRawAtMs: number;
}

export function initPositionInterpolatorState(): PositionInterpolatorState {
	return { lastRawPositionMs: null, lastRawAtMs: 0 };
}

/**
 * Return the position to paint this frame, mutating `state` in place to
 * re-anchor whenever `raw` has moved.
 *
 * @param state   Per-deck interpolator memory (one instance per WaveRow).
 * @param raw     The latest raw `deck.position_ms` (ground truth).
 * @param nowMs   The caller's own "now" (e.g. `performance.now()`).
 * @param playing Whether the deck is actually playing right now.
 * @param trusted Whether the presentation clock is currently trusted (false
 *                while frozen/stalled) -- interpolating a stalled clock would
 *                hide the exact failure `playheadFrozen` exists to surface.
 * @param rate    Track-ms advanced per real ms (e.g. `deck.pitch`).
 * @param sampledAtMs When the engine sampled `raw` (`performance.now()`), or
 *                null when unknown; see the module note above.
 */
export function interpolatedPositionMs(
	state: PositionInterpolatorState,
	raw: number,
	nowMs: number,
	playing: boolean,
	trusted: boolean,
	rate: number,
	sampledAtMs: number | null
): number {
	if (raw !== state.lastRawPositionMs) {
		state.lastRawPositionMs = raw;
		state.lastRawAtMs = nowMs;
	}
	if (sampledAtMs !== null) state.lastRawAtMs = sampledAtMs;
	if (!playing || !trusted) return raw;
	return raw + (nowMs - state.lastRawAtMs) * rate;
}

/**
 * A scrub in progress wins outright (the user's own drag is ground truth,
 * never interpolated), else defer to `interpolatedPositionMs` above.
 */
export function paintPositionMsFor(
	state: PositionInterpolatorState,
	scrubPreviewMs: number | null,
	raw: number,
	nowMs: number,
	playing: boolean,
	trusted: boolean,
	rate: number,
	sampledAtMs: number | null
): number {
	if (scrubPreviewMs !== null) return scrubPreviewMs;
	return interpolatedPositionMs(state, raw, nowMs, playing, trusted, rate, sampledAtMs);
}

/** The subset of `DeckState` `paintPositionMs` below actually reads. */
export interface PaintPositionDeck {
	position_ms: number;
	playing: boolean;
	pitch: number;
}

/**
 * `WaveRow.svelte`'s actual per-frame paint call, reshaped around its own
 * locals (`deck`, `clockUntrusted`) rather than `paintPositionMsFor`'s flat
 * argument list, so the call site stays a single readable line instead of
 * either a 130+ character line or a hand-wrapped one bought back just to
 * satisfy a line-count budget.
 */
export function paintPositionMs(
	state: PositionInterpolatorState,
	scrubPreviewMs: number | null,
	deck: PaintPositionDeck,
	clockUntrusted: boolean,
	nowMs: number,
	sampledAtMs: number | null
): number {
	const { position_ms: raw, playing, pitch } = deck;
	return paintPositionMsFor(state, scrubPreviewMs, raw, nowMs, playing, !clockUntrusted, pitch, sampledAtMs);
}

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
