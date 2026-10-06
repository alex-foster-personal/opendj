/**
 * What each animated element actually painted, for the smoothness probe.
 *
 * Every element that moves with a deck's playhead (strip waveform, split-row
 * partner, jog phase marks, overview playhead) reports the position it just
 * rendered from. A no-op unless a probe has installed
 * `globalThis.__mdtPlayheadTrace` (an object) before the app loaded, so the
 * shipped cost is one property read per paint.
 *
 * The probe (`tests/live/playhead-smoothness.mjs`) reads the object once
 * per frame and scores each element by how far its frame-to-frame movement
 * strays from tempo x frame interval. Keys are `<element>:<deck>`; clock events
 * are counted under `event:<kind>:<deck>`.
 */
import type { DeckId } from '$lib/rb/deck-id';

type PlayheadTrace = Record<string, number>;

function _trace(): PlayheadTrace | undefined {
	return (globalThis as { __mdtPlayheadTrace?: PlayheadTrace }).__mdtPlayheadTrace;
}

export function tracePlayhead(element: string, deck: DeckId, positionMs: number): void {
	const trace = _trace();
	if (trace !== undefined) trace[`${element}:${deck}`] = positionMs;
}

export function tracePlayheadEvent(kind: string, deck: DeckId): void {
	const trace = _trace();
	if (trace === undefined) return;
	const key = `event:${kind}:${deck}`;
	trace[key] = (trace[key] ?? 0) + 1;
}
