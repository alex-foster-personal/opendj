/**
 * Pure helpers bridging a beatgrid-fallback response into the existing
 * wavestack painter (build unit: wavestack). No runes, no DOM - unit-
 * testable in isolation. ANLZ always preferred, never invented.
 */
import type { BeatgridFallbackOut } from './beatgrid-fallback-api';
import type { AnlzData } from './types';

/** True only once GET /anlz has confirmed no rekordbox ANLZ exists for
 * this track. This is the single gate a caller should check before ever
 * fetching the fallback grid - ANLZ is always preferred. */
export function shouldUseBeatgridFallback(anlzErrorCode: string | null): boolean {
	return anlzErrorCode === 'ANALYSIS_NOT_FOUND';
}

/** Wrap a beatgrid-fallback response in an AnlzData-shaped payload so
 * drawWaveRow / barsToNextCueLabel render it unmodified: real beat ticks
 * plus a bars-to-grid-end countdown. Waveform bands stay empty (no
 * fabricated audio), cues/phrases stay empty (this pipeline proposes
 * neither), and vocals is reported honestly as 'not_analyzed' (this
 * pipeline never touches PVDI). */
export function toSyntheticAnlzData(
	fallback: BeatgridFallbackOut
): AnlzData & { vocals: { status: 'not_analyzed' } } {
	return {
		stable_id: fallback.stable_id,
		points: 0,
		waveform: {
			kind: 'mono',
			preview: { length: 0, low: [], mid: [], high: [] },
			detail: { length: 0, low: [], mid: [], high: [] }
		},
		beatgrid: fallback.beatgrid,
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}
