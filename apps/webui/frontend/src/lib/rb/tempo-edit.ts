import type { DeckId } from '$lib/rb/deck-slots';
import { PITCH_RANGES, type PitchRange } from '$lib/player/constants';

export function parseTempoBpmInput(raw: string): number | null {
	const trimmed = raw.trim();
	if (trimmed === '') return null;
	const value = Number(trimmed);
	if (!Number.isFinite(value) || value <= 0) return null;
	return value;
}

export function tempoRatioFromTargetBpm(targetBpm: number, baseBpm: number): number {
	if (!Number.isFinite(targetBpm) || targetBpm <= 0) {
		throw new RangeError(`targetBpm must be finite and positive, got ${targetBpm}`);
	}
	if (!Number.isFinite(baseBpm) || baseBpm <= 0) {
		throw new RangeError(`baseBpm must be finite and positive, got ${baseBpm}`);
	}
	return targetBpm / baseBpm;
}

export function smallestPitchRangeForRatio(ratio: number): PitchRange | null {
	if (!Number.isFinite(ratio) || ratio <= 0) return null;
	const deviationPct = Math.abs(ratio - 1) * 100;
	for (const range of PITCH_RANGES) {
		if (deviationPct <= range + 1e-9) return range;
	}
	return null;
}

export function halveDoubleVisibility(effectiveBpm: number): { halve: boolean; double: boolean } {
	if (!Number.isFinite(effectiveBpm)) return { halve: false, double: false };
	return {
		halve: effectiveBpm >= 150,
		double: effectiveBpm >= 75 && effectiveBpm <= 100
	};
}

export function nudgeBpm(currentBpm: number, delta: number): number {
	return currentBpm + delta;
}

export type TempoWrite =
	| { type: 'pitch_range'; deck: DeckId; range: PitchRange }
	| { type: 'tempo'; deck: DeckId; ratio: number };

export function tempoWritesFromTargetBpm(input: {
	deck: DeckId;
	targetBpm: number;
	baseBpm: number;
	currentRange: PitchRange;
}): TempoWrite[] | null {
	const ratio = tempoRatioFromTargetBpm(input.targetBpm, input.baseBpm);
	const neededRange = smallestPitchRangeForRatio(ratio);
	if (neededRange === null) return null;
	const writes: TempoWrite[] = [];
	if (neededRange > input.currentRange) {
		writes.push({ type: 'pitch_range', deck: input.deck, range: neededRange });
	}
	writes.push({ type: 'tempo', deck: input.deck, ratio });
	return writes;
}

/** DECKUX-13420: why the engine refuses every tempo write on this deck right
 * now, or null when it takes them. Mirrors setTempoRatio in both engines: a
 * PLAYING Beat Sync follower of a playing master is held to the master's
 * tempo (spec f1345d8c, "Beat Sync followers": the DJ turns Beat Sync off and
 * retries). A stopped follower changes tempo, and widens its pitch range, like
 * any other deck. `syncMaster` is the playing master the engine locks to. */
export function followerTempoLock(input: {
	deck: DeckId;
	playing: boolean;
	beatSync: boolean;
	syncMaster: DeckId | null;
}): string | null {
	const { deck, syncMaster } = input;
	if (!input.playing || !input.beatSync || syncMaster === null || syncMaster === deck) return null;
	return `Tempo locked while CH${deck} follows CH${syncMaster} with Beat Sync: disable Beat Sync before changing follower deck ${deck}`;
}

/** Send `writes` in order through `run`; true only when every one was taken.
 * The first refused write (a preset owns the controls, the deck unloaded)
 * stops the rest, so a tempo is never sent against a range that was refused. */
export async function runTempoWrites(
	writes: readonly TempoWrite[],
	run: (write: TempoWrite) => Promise<{ ok: boolean }>
): Promise<boolean> {
	for (const write of writes) {
		if (!(await run(write)).ok) return false;
	}
	return true;
}

/** A gap longer than this between taps starts a new tap run. */
export const TAP_TEMPO_RESET_MS = 2000;
/** Taps needed before a tapped tempo is applied (three intervals). */
export const TAP_TEMPO_MIN_TAPS = 4;
/** The tempo is the mean of at most this many of the latest intervals. */
export const TAP_TEMPO_WINDOW = 8;

/** I12 (TEMPO-SRC-01): add one tap at `nowMs` to a tap run. A gap longer
 * than TAP_TEMPO_RESET_MS, or a clock that went backwards, starts a new run
 * rather than averaging across it. Keeps only the taps the window needs. */
export function pushTempoTap(taps: readonly number[], nowMs: number): number[] {
	if (!Number.isFinite(nowMs)) throw new RangeError(`tap time must be finite, got ${nowMs}`);
	const last = taps.length > 0 ? taps[taps.length - 1] : null;
	if (last === null || nowMs <= last || nowMs - last > TAP_TEMPO_RESET_MS) return [nowMs];
	return [...taps, nowMs].slice(-(TAP_TEMPO_WINDOW + 1));
}

/** The tempo a tap run marks, or null until TAP_TEMPO_MIN_TAPS taps landed. */
export function tapTempoBpm(taps: readonly number[]): number | null {
	if (taps.length < TAP_TEMPO_MIN_TAPS) return null;
	const recent = taps.slice(-(TAP_TEMPO_WINDOW + 1));
	const spanMs = recent[recent.length - 1] - recent[0];
	if (!(spanMs > 0)) return null;
	return (60000 * (recent.length - 1)) / spanMs;
}
