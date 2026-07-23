/**
 * Beat-4 lead-in for synced waveform seeks that snap backwards.
 * PQTZ n cycles 1..4; the n=4 immediately at/before the landing is the
 * last beat of the prior bar (or the landing itself when already on 4).
 */

export interface BeatGridBeat {
	t: number;
	n: number;
}

/** Time of the beat-4 at or before `targetSec`, or null when none exists. */
export function beatFourLeadInSec(
	beats: readonly BeatGridBeat[],
	targetSec: number
): number | null {
	if (!Number.isFinite(targetSec) || targetSec < 0) {
		throw new RangeError(`targetSec must be finite and >= 0, got ${targetSec}`);
	}
	if (beats.length < 2) return null;
	let lo = 0;
	let hi = beats.length - 1;
	while (lo < hi) {
		const mid = Math.ceil((lo + hi) / 2);
		if (beats[mid].t <= targetSec) lo = mid;
		else hi = mid - 1;
	}
	if (beats[lo].t > targetSec) return null;
	for (let i = lo; i >= 0; i -= 1) {
		if (beats[i].n === 4) return beats[i].t;
	}
	return null;
}

/** Wall-clock blend length so playback from `incomingSec` reaches `landingSec`. */
export function syncSeekBlendDurationSec(
	incomingSec: number,
	landingSec: number,
	tempoRatio: number
): number {
	if (!Number.isFinite(tempoRatio) || tempoRatio <= 0) {
		throw new RangeError(`tempoRatio must be finite and > 0, got ${tempoRatio}`);
	}
	if (!Number.isFinite(incomingSec) || !Number.isFinite(landingSec)) {
		throw new RangeError('incomingSec and landingSec must be finite');
	}
	if (landingSec <= incomingSec) return 0.35;
	return Math.min(0.85, Math.max(0.22, (landingSec - incomingSec) / tempoRatio));
}
