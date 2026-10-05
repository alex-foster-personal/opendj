/**
 * The per-deck playhead clocks every animated element draws from (ANIM-CLOCK-01).
 *
 * Both engines feed it once per published tick (`notePositionSample`); the
 * strip waveform and its split-row partner, the jog dial and the overview
 * playhead all read `playheadMs`, so every element moves on the same clock and
 * synced decks stay in phase on screen whichever element's frame ran first.
 * The arithmetic, thresholds and reasoning are in `playhead-clock.ts`.
 */
import type { PresentedTransportObservation } from '$lib/player/transport/presentation';
import type { DeckId } from '$lib/rb/deck-id';
import { initPlayheadClock, notePlayheadSample, playheadDisplayMs, projectionCapped, type PlayheadClockState } from '$lib/rb/playhead-clock';
import { tracePlayhead, tracePlayheadEvent } from '$lib/rb/playhead-trace';
import { isPresentationClockStalled } from '$lib/rb/presentation-clock-report';

/** The deck fields a sample and a read need (a `DeckState` satisfies it). */
export interface PlayheadDeckView {
	position_ms: number;
	/** Desired transport. */
	playing: boolean;
	/** Presented output state: a deck whose start has not reached the speakers yet is not projected. */
	audible: boolean;
	pitch: number;
	stable_id: string | null;
}

const _clocks: Record<DeckId, PlayheadClockState> = { 1: initPlayheadClock(), 2: initPlayheadClock(), 3: initPlayheadClock(), 4: initPlayheadClock() };
// Bumped on every sample so DOM-bound readers re-derive even when a repeated
// output timestamp republishes an identical position_ms (which Svelte ignores).
const _revision: Record<DeckId, number> = $state({ 1: 0, 2: 0, 3: 0, 4: 0 });

/**
 * The current frame's time: `document.timeline.currentTime` is the rAF
 * timestamp of the frame being produced, the same for every element in it, so
 * elements painted early or late within one frame still agree. Falls back to
 * `performance.now()` where there is no document timeline (node tests).
 */
function _frameNowMs(): number {
	const current = typeof document === 'undefined' ? null : document.timeline?.currentTime;
	return typeof current === 'number' ? current : performance.now();
}

/**
 * When the presented position was true: the output timestamp's
 * `performanceTime` while the device clock drives presentation, else (the
 * sample-clock fallback, or an unreadable timestamp) the moment it was read.
 */
export function presentedSampleAtMs(
	observation: Pick<PresentedTransportObservation, 'clock_source'>,
	outputTimestamp: { performanceTime: number },
	nowMs: number = performance.now()
): number {
	const { performanceTime } = outputTimestamp;
	const usable = observation.clock_source === 'output' && Number.isFinite(performanceTime) && performanceTime >= 0;
	return usable ? performanceTime : nowMs;
}

/** One authoritative position for `deck`, true at `sampledAtMs`. */
export function notePositionSample(deck: DeckId, view: PlayheadDeckView, sampledAtMs: number, nowMs: number = _frameNowMs()): void {
	const sample = {
		positionMs: view.position_ms,
		sampledAtMs,
		rate: view.pitch,
		playing: view.playing && view.audible,
		trusted: !isPresentationClockStalled(deck),
		trackKey: view.stable_id
	};
	tracePlayheadEvent(notePlayheadSample(_clocks[deck], sample, nowMs), deck);
	tracePlayhead('age', deck, nowMs - sampledAtMs);
	_revision[deck] += 1;
}

/**
 * The position to draw `deck` at now. A deck that is not playing, or whose
 * clock has no playing sample yet, draws its published position exactly: a
 * paused cursor, a seek while paused and a fresh load are never projected.
 */
export function playheadMs(deck: DeckId, view: Pick<PlayheadDeckView, 'position_ms' | 'playing'>, nowMs: number = _frameNowMs()): number {
	void _revision[deck];
	const clock = _clocks[deck];
	if (!view.playing || clock.anchor === null || !clock.anchor.playing) return view.position_ms;
	if (projectionCapped(clock.anchor, nowMs)) tracePlayheadEvent('capped', deck);
	return playheadDisplayMs(clock, nowMs) ?? view.position_ms;
}
