// reanalyze-grid.ts -- ANALYSIS-11723: the analysis dots popover's
// "Re-analyze grid" item, kept pure so a unit test can drive it with only
// fetch stubbed. It writes (or clears) the track's own BPM range FIRST, then
// orders the beatgrid re-analysis; a refused range write orders nothing.

import { orderTrackAnalysis, type AnalysisOrder } from './api-ingest';
import { clearAnalysisRange, putAnalysisRange } from './api-rb';
import { runAnalysisOrder } from './analysis-order';

/** Mirrors apps/shared/state/analysis_range.py BPM_RANGES (a Python test guards the drift). */
export const BPM_RANGES = [
	'70-180',
	'60-120',
	'75-150',
	'85-170',
	'93-186',
	'100-200',
	'120-240'
] as const;
export const GLOBAL_RANGE = '70-180';

export type ReanalyzeDeps = {
	upsertJob: (phase: AnalysisOrder['phase']) => void;
	toast: (message: string, kind: 'info' | 'error') => void;
};

/** Writes the track's range (null = the global band), THEN orders the beatgrid re-run. */
export async function reanalyzeGrid(
	stableId: string,
	range: string | null,
	deps: ReanalyzeDeps
): Promise<void> {
	try {
		if (range === null) await clearAnalysisRange(stableId);
		else await putAnalysisRange(stableId, range);
	} catch (e) {
		deps.toast(`BPM range not saved: ${e instanceof Error ? e.message : String(e)}`, 'error');
		return;
	}
	await runAnalysisOrder('beatgrid', {
		orderTrackAnalysis: () => orderTrackAnalysis(stableId, 'beatgrid'),
		upsertJob: deps.upsertJob,
		toast: deps.toast
	});
}
