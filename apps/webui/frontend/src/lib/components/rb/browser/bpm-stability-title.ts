import type { GridQualityRow } from '$lib/rb/analysis-issues';

/** Hover title of the BPM Stability cell (issue #7879): the daemon's tempo
 * sentence, plus the grid message for a suspect grid. Undefined when the tempo
 * was never judged (blank cell). Joins served strings only. */
export function bpmStabilityTitle(grid: GridQualityRow | null | undefined): string | undefined {
	const tempo = grid?.tempo ?? null;
	if (tempo === null) return undefined;
	if (grid?.grid_class === 'suspect' && grid.message) return `${tempo.title} ${grid.message}`;
	return tempo.title;
}
