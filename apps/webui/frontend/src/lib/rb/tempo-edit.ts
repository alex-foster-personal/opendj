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
