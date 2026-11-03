/**
 * Main-waveform zoom view math (WAVEZOOM-6802..06): the window each step shows,
 * where the playhead sits, and the step glide. Only WaveRow loads it, so it
 * stays off the "/" first paint; the ladder and prefs are in wave-zoom.ts.
 *
 * Window seconds here are WALL-CLOCK, like the old WAVE_WINDOW_S: render.ts
 * scales them by each deck's pitch into track time, so synced decks keep the
 * same beat spacing at every step.
 */
import type { DeckId } from '$lib/player/constants';
import type { WavePlayheadMode, WaveZoom } from './wave-zoom';

/** Seconds per bar when no reference deck has a BPM: 1.5 s, so the default
 * 16 bars is exactly the old fixed 24 s window. */
export const WAVE_ZOOM_FALLBACK_S_PER_BAR = 1.5;

/** One zoom step (key, chip or wheel notch) animates for this long. */
export const WAVE_ZOOM_ANIM_MS = 220;

/** Wheel delta (px) that counts as one zoom step. A notched mouse sends about
 * 100 per detent; a trackpad pinch arrives as many small ctrl-wheel deltas. */
export const WAVE_ZOOM_WHEEL_STEP_PX = 60;

/** The chip's text, e.g. "16 BARS" or "WHOLE TRACK". */
export function waveZoomLabel(zoom: WaveZoom): string {
	return zoom === 'whole' ? 'WHOLE TRACK' : `${zoom} BARS`;
}

/** The deck the zoom is measured against: its BPM sizes a bar and its length
 * is "whole track". The master when it is loaded, else the first loaded deck. */
export interface WaveZoomRef {
	bpm: number | null;
	pitch: number;
	durationMs: number | null;
	positionMs: number;
}

export interface WaveZoomDeck extends WaveZoomRef {
	id: DeckId;
	loaded: boolean;
	isMaster: boolean;
}

/** The loaded master, else the first loaded deck. Election is by identity: a
 * reference with bad metadata fails in the zoom math, it never hands the
 * reference to another deck. */
export function waveZoomRef<T extends WaveZoomDeck>(decks: readonly T[]): T | null {
	const loaded = decks.filter((d) => d.loaded);
	return loaded.find((d) => d.isMaster) ?? loaded[0] ?? null;
}

/** The reference's pitch, which every zoom length divides by. An invalid pitch
 * is a broken contract, not a missing reference, so it throws rather than
 * falling back to the 24 s view. */
function _referencePitch(ref: WaveZoomRef): number {
	if (!Number.isFinite(ref.pitch) || ref.pitch <= 0) {
		throw new RangeError(`wave zoom: reference pitch must be finite and > 0, got ${ref.pitch}`);
	}
	return ref.pitch;
}

/** The reference's length in ms. A reference with no usable length is a broken
 * contract: the whole-track view and the playhead both need it, so it throws
 * rather than painting a synthetic window. */
function _referenceDurationMs(ref: WaveZoomRef): number {
	if (ref.durationMs === null || !Number.isFinite(ref.durationMs) || ref.durationMs <= 0) {
		throw new RangeError(`wave zoom: reference duration must be finite and > 0, got ${ref.durationMs}`);
	}
	return ref.durationMs;
}

function _secondsPerBar(ref: WaveZoomRef | null): number {
	if (ref === null) return WAVE_ZOOM_FALLBACK_S_PER_BAR;
	const pitch = _referencePitch(ref);
	// Only a genuinely absent BPM takes the fallback; a present but malformed one is corrupt state.
	if (ref.bpm === null) return WAVE_ZOOM_FALLBACK_S_PER_BAR;
	if (!Number.isFinite(ref.bpm) || ref.bpm <= 0) {
		throw new RangeError(`wave zoom: reference bpm must be finite and > 0, got ${ref.bpm}`);
	}
	// 4/4: a bar is four beats at the reference deck's PLAYING tempo.
	return 240 / (ref.bpm * pitch);
}

function _wholeS(ref: WaveZoomRef | null): number | null {
	if (ref === null) return null;
	return _referenceDurationMs(ref) / 1000 / _referencePitch(ref);
}

/** Wall-clock seconds the row shows at this step. A bar step never shows
 * more than the whole reference track, so on a short track the ladder still
 * only ever widens toward "whole" (32 and 64 bars may both equal it). */
export function waveZoomWindowS(zoom: WaveZoom, ref: WaveZoomRef | null): number {
	const wholeS = _wholeS(ref);
	if (zoom === 'whole') return wholeS ?? 64 * _secondsPerBar(ref);
	const barsS = Number(zoom) * _secondsPerBar(ref);
	return wholeS === null ? barsS : Math.min(barsS, wholeS);
}

function _smoothstep(u: number): number {
	const t = Math.min(1, Math.max(0, u));
	return t * t * (3 - 2 * t);
}

/** Where the playhead sits across the row, 0..1. Elastic: 0.5 until the
 * window passes half the reference track, then a smoothstep glide to the
 * reference's own place in its track, reached when the window fits the whole
 * track. Every deck uses the same fraction, so beats still line up. */
export function wavePlayheadFrac(mode: WavePlayheadMode, windowS: number, ref: WaveZoomRef | null): number {
	if (mode === 'center' || ref === null) return 0.5;
	const durationMs = _referenceDurationMs(ref);
	const durS = durationMs / 1000 / _referencePitch(ref);
	const blend = _smoothstep((windowS - durS / 2) / (durS / 2));
	const place = Math.min(1, Math.max(0, ref.positionMs / durationMs));
	return 0.5 + (place - 0.5) * blend;
}

/** A zoom step in flight: from one step to another, started at startMs.
 * fromWindowS is set when a step interrupts a glide: the glide then starts
 * from the window on screen, not from the previous step's target. */
export interface WaveZoomAnim {
	from: WaveZoom;
	to: WaveZoom;
	startMs: number;
	fromWindowS?: number | undefined;
}

function _easeOutCubic(t: number): number {
	return 1 - (1 - t) ** 3;
}

/** Whether the step animation is still running at nowMs. */
export function waveZoomAnimating(anim: WaveZoomAnim | null, nowMs: number, durationMs = WAVE_ZOOM_ANIM_MS): boolean {
	return anim !== null && anim.from !== anim.to && nowMs - anim.startMs < durationMs;
}

/** Where a glide starts: the window on screen when it interrupted another
 * glide, else the live window of its from step. */
export function waveZoomAnimFromS(anim: WaveZoomAnim, ref: WaveZoomRef | null): number {
	return anim.fromWindowS ?? waveZoomWindowS(anim.from, ref);
}

/** The beatgrid's local tempo at a track time: the bpm of the last beat at or
 * before timeS, the first beat's before the grid starts. Null only when the grid
 * is empty, so the caller falls back to the track's BPM. A beat with a
 * non-finite or non-positive bpm is corrupt grid data and throws. */
export function beatgridTempoAt(beats: readonly { t: number; bpm: number }[], timeS: number): number | null {
	if (beats.length === 0) return null;
	let low = 0;
	let high = beats.length - 1;
	while (low < high) {
		const middle = (low + high + 1) >> 1;
		if (beats[middle].t <= timeS) low = middle;
		else high = middle - 1;
	}
	const bpm = beats[low].bpm;
	if (!Number.isFinite(bpm) || bpm <= 0) {
		throw new RangeError(`wave zoom: beatgrid bpm must be finite and > 0, got ${bpm}`);
	}
	return bpm;
}

/** Whether a beatgrid's tempo changes along the track. Beats whose bpm differs
 * from the first by more than a hundredth of a beat per minute are a variable
 * grid; a constant grid, or none, never changes the bar length. */
export function beatgridVariesTempo(beats: readonly { t: number; bpm: number }[] | null): boolean {
	if (beats === null || beats.length < 2) return false;
	const first = beats[0].bpm;
	return beats.some((beat) => Math.abs(beat.bpm - first) > 0.01);
}

/** The reference deck at a position: its bar tempo is the local tempo of the
 * beats its row paints there (`beats`), falling back to the reference's track BPM. */
export function waveZoomRefAt(
	ref: WaveZoomDeck,
	positionMs: number,
	beats: readonly { t: number; bpm: number }[] | null,
): WaveZoomRef {
	const gridBpm = beats === null ? null : beatgridTempoAt(beats, positionMs / 1000);
	return { ...ref, positionMs, bpm: gridBpm ?? ref.bpm };
}

/** The latest glide of each deck's row, shared by every row. A row that is not
 * the reference places the shared playhead from the reference's animated window
 * (WAVEZOOM-6802c), so the reference's glide must be readable from any row. */
const _glides = new Map<DeckId, { anim: WaveZoomAnim; durationMs: number }>();

export function setWaveZoomGlide(deckId: DeckId, anim: WaveZoomAnim, durationMs: number): void {
	_glides.set(deckId, { anim, durationMs });
}

/** The window the shared playhead is placed from: the deck's glide to `step`
 * while it animates, else the live window of `step`. */
export function waveZoomSharedWindowS(deckId: DeckId, step: WaveZoom, ref: WaveZoomRef | null, nowMs: number): number {
	const glide = _glides.get(deckId);
	if (glide !== undefined && glide.anim.to === step && waveZoomAnimating(glide.anim, nowMs, glide.durationMs)) {
		return waveZoomAnimatedWindowS(glide.anim, ref, nowMs, glide.durationMs);
	}
	return waveZoomWindowS(step, ref);
}

/** The window at nowMs: eased in LOG space between the two steps' live
 * windows, so every step feels the same speed (4 to 8 bars as 64 to whole).
 * Both ends are recomputed from the live reference, so a tempo change mid
 * animation never snaps. durationMs 0 (reduced motion) jumps. */
export function waveZoomAnimatedWindowS(
	anim: WaveZoomAnim,
	ref: WaveZoomRef | null,
	nowMs: number,
	durationMs = WAVE_ZOOM_ANIM_MS
): number {
	const to = waveZoomWindowS(anim.to, ref);
	if (!waveZoomAnimating(anim, nowMs, durationMs)) return to;
	const from = waveZoomAnimFromS(anim, ref);
	const t = _easeOutCubic(Math.max(0, (nowMs - anim.startMs) / durationMs));
	return Math.exp(Math.log(from) + (Math.log(to) - Math.log(from)) * t);
}

/** A wheel event's vertical delta in pixels. Line mode (1) is about 40 px a line
 * and page mode (2) is one whole canvas height, so a page-mode notch of 1 still
 * counts as a step. Pixel mode (0) passes through. */
export function waveZoomWheelDeltaPx(deltaY: number, deltaMode: number, pageHeightPx: number): number {
	if (deltaMode === 1) return deltaY * 40;
	if (deltaMode === 2) return deltaY * pageHeightPx;
	return deltaY;
}

/** Wheel accumulator: returns the signed number of zoom steps this delta
 * completes, one per WAVE_ZOOM_WHEEL_STEP_PX crossed, and keeps the rest for
 * the next event. Wheel up (negative deltaY) zooms in, so it returns positive. */
export function waveZoomWheelSteps(acc: { px: number }, deltaY: number): number {
	if (deltaY === 0) return 0;
	if (Math.sign(deltaY) !== Math.sign(acc.px)) acc.px = 0;
	acc.px += deltaY;
	const crossed = Math.trunc(acc.px / WAVE_ZOOM_WHEEL_STEP_PX);
	acc.px -= crossed * WAVE_ZOOM_WHEEL_STEP_PX;
	return -crossed || 0;
}
