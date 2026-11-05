/**
 * Tempo disputes between our beat analysis and Mixed In Key (GRID-OCTAVE-02).
 *
 * The server keeps ONE BPM (ours for an octave disagreement and for one over
 * 6%) and marks it `bpm_disputed` with both tempos in `bpm_alternatives`. The
 * library BPM cell and the deck BPM readout both show the same marker and the
 * same hover line, built here, and both offer the other tempo through
 * `PUT /api/v1/tracks/{id}/bpm-pick` (agents: `opendj track bpm-pick`, MCP
 * `track_bpm_pick`).
 */

export type BpmAlternativeSource = 'own' | 'mik';

export interface BpmAlternative {
	source: BpmAlternativeSource;
	bpm: number;
}

const LABEL: Record<BpmAlternativeSource, string> = { own: 'our analysis', mik: 'Mixed In Key' };

function bpmOf(alternatives: readonly BpmAlternative[], source: BpmAlternativeSource): BpmAlternative | null {
	return alternatives.find((a) => a.source === source) ?? null;
}

/** Which of the two tempos the shown BPM is (nearest wins; they differ by 6% or more). */
export function shownAlternative(alternatives: readonly BpmAlternative[], shownBpm: number | null): BpmAlternative | null {
	if (alternatives.length === 0) return null;
	if (shownBpm === null) return bpmOf(alternatives, 'own');
	return alternatives.reduce((best, a) => (Math.abs(a.bpm - shownBpm) < Math.abs(best.bpm - shownBpm) ? a : best));
}

/** The tempo the hover card's button switches to; null when there is no dispute to settle. */
export function otherAlternative(alternatives: readonly BpmAlternative[], shownBpm: number | null): BpmAlternative | null {
	const shown = shownAlternative(alternatives, shownBpm);
	return shown === null ? null : (alternatives.find((a) => a.source !== shown.source) ?? null);
}

/**
 * "Mixed In Key: 93.75 BPM, our analysis: 129.59 BPM (confidence 0.0). Showing ours."
 * Null when the tempos agree (no alternatives).
 */
export function bpmDisputeText(
	alternatives: readonly BpmAlternative[],
	shownBpm: number | null,
	confidence: number | null | undefined
): string | null {
	const mik = bpmOf(alternatives, 'mik');
	const own = bpmOf(alternatives, 'own');
	const shown = shownAlternative(alternatives, shownBpm);
	if (mik === null || own === null || shown === null) return null;
	const conf = confidence === null || confidence === undefined ? 'confidence unknown' : `confidence ${confidence.toFixed(1)}`;
	const showing = shown.source === 'own' ? 'Showing ours.' : "Showing Mixed In Key's.";
	return `Mixed In Key: ${mik.bpm.toFixed(2)} BPM, our analysis: ${own.bpm.toFixed(2)} BPM (${conf}). ${showing}`;
}

/** The hover card button's label for switching to `alt`. */
export function useAlternativeLabel(alt: BpmAlternative): string {
	return alt.source === 'mik' ? `Use Mixed In Key's ${alt.bpm.toFixed(2)} BPM` : `Use ${LABEL.own}: ${alt.bpm.toFixed(2)} BPM`;
}
