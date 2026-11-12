/**
 * Tempo / key cell editor (pin a720a02ecf92).
 * The key the cell commits is re-canonicalized server-side (canonical_key_text,
 * KEY-10229) and the server's answer is what the cell keeps.
 * Tap tempo is the mean gap between taps. A gap over TAP_GAP_RESET_MS, or a
 * clock that jumps backwards, starts a new run. One tap is not a tempo.
 */
import { parseCamelotKey } from '$lib/player/key/camelot';

export const TAP_GAP_RESET_MS = 2000;
export const TAP_HISTORY = 8;

export interface TapTempoState {
	timesMs: number[];
}

export function emptyTapTempo(): TapTempoState {
	return { timesMs: [] };
}

export function bpmFromIntervals(intervalsMs: readonly number[]): number | null {
	if (intervalsMs.length === 0) return null;
	let sum = 0;
	for (const gap of intervalsMs) {
		if (!Number.isFinite(gap) || gap <= 0) return null;
		sum += gap;
	}
	const bpm = 60000 / (sum / intervalsMs.length);
	if (!Number.isFinite(bpm) || bpm <= 0) return null;
	return Math.round(bpm * 100) / 100;
}

export function tapTempoAt(
	state: TapTempoState,
	nowMs: number
): { state: TapTempoState; bpm: number | null } {
	if (!Number.isFinite(nowMs)) throw new RangeError(`nowMs must be finite, got ${nowMs}`);
	const last = state.timesMs.at(-1);
	const fresh = last === undefined || nowMs < last || nowMs - last > TAP_GAP_RESET_MS;
	const timesMs = (fresh ? [nowMs] : [...state.timesMs, nowMs]).slice(-TAP_HISTORY);
	const intervalsMs: number[] = [];
	for (let i = 1; i < timesMs.length; i++) intervalsMs.push(timesMs[i] - timesMs[i - 1]);
	return { state: { timesMs }, bpm: bpmFromIntervals(intervalsMs) };
}

export function formatDraftBpm(bpm: number): string {
	return (Math.round(bpm * 100) / 100).toFixed(2);
}

/** Canonical Camelot label (`8A`), or null when the spelling is neither Camelot nor a note name. */
export function canonicalKeyLabel(raw: string): string | null {
	const parsed = parseCamelotKey(raw);
	if (parsed === null) return null;
	return `${parsed.number}${parsed.mode}`;
}

/**
 * Shortest chromatic step from `fromKey` to `toKey` (-6..6).
 * 0 means the roots already match. Null when either spelling does not parse.
 * Major/minor is not a nudge: the deck keeps its mode and only the pitch moves.
 */
export function semitoneNudgeBetween(fromKey: string | null, toKey: string): number | null {
	const from = parseCamelotKey(fromKey);
	const to = parseCamelotKey(toKey);
	if (from === null || to === null) return null;
	let delta = to.root - from.root;
	if (delta > 6) delta -= 12;
	if (delta < -6) delta += 12;
	return delta;
}
