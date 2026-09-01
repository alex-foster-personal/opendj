/**
 * Err-column mapping: cached backend diagnostics -> lit Err dots.
 *
 * The Err column colours a slot ONLY where a real detector produced a real
 * finding. Beatgrid is the only analysis kind with a detector today (see
 * `apps/webui/server/rb_vendor.cached_beatgrid_issue`), so the other eight
 * slots stay off rather than fabricate a state - the house rule is no mocked
 * data ever, and a guessed error state is mocked data pointed at the DJ.
 *
 * Extracted from TrackTable.svelte so the rule is unit-testable: it runs per
 * visible row over thousands of rows and must never parse a full ANLZ
 * beat-grid here.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Only kinds with a real detector may light.
 *     [if] a row has no rb_meta, or rb_meta.beatgrid_issue is null [then] the
 *       map is empty and the Err grid renders all-off ⛔️ any dot lights
 *     [if] a row has a real beatgrid issue [then] exactly one slot lights,
 *       carrying its severity plus the numeric disagreement and timestamp
 *       ⛔️ a bare colour with no detail
 *     [if] a new AnalysisKind is added without a detector [then] its Err slot
 *       stays off ⛔️ a "done" badge is reused as an issue signal
 */
import type { AnalysisIssues } from '$lib/rb/job-progress.svelte';
import type { BeatgridIssue } from '$lib/rb/library-types';

/** Analysis kinds that have a working detector behind them, today. */
export const DETECTED_ANALYSIS_KINDS = ['beatgrid'] as const;

/** The row fields this mapping reads. Kept structural so both BrowserRow and
 * a bare rb-meta payload can be passed without a cast. */
export interface AnalysisIssueSource {
	rb_meta?: { beatgrid_issue?: BeatgridIssue | null } | null;
}

/** Err-column issues for one library row. Empty map = nothing detected. */
export function analysisIssuesFor(row: AnalysisIssueSource): AnalysisIssues {
	const issue = row.rb_meta?.beatgrid_issue;
	if (issue === null || issue === undefined) return {};
	return {
		beatgrid: {
			severity: issue.severity,
			detail:
				`Beatgrid: PQTZ field BPM disagrees with beat interval by ` +
				`${issue.disagreement_bpm.toFixed(1)} BPM near t=${Math.round(issue.at_sec)}s - ` +
				`can cause Beat Sync tempo to jump on seek near this point`
		}
	};
}
