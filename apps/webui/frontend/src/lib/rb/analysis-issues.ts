/**
 * Err-column mapping: cached backend diagnostics -> lit Err dots.
 *
 * The Err column colours a slot ONLY where a real detector produced a real
 * finding. Beatgrid is the only analysis kind with a detector today: the
 * stored beatgrid verdict on the row (GRIDFLAG-02, the same rule as the
 * deck's Beat Sync badge), with the older field-vs-interval detector
 * (`apps/webui/server/rb_vendor.cached_beatgrid_issue`) as the fallback for
 * a grid nobody could judge. So the other eight
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
 *   ✔︎ ✅ 🎯 The beatgrid flag (GRIDFLAG-03, GRIDFLAG-04).
 *     [if] the row's grid is `suspect` [then] a warning dot with the measured
 *       sentence ⛔️ a bare color
 *     [if] the grid is `unknown` [then] a hollow `unknown` dot ⛔️ drawn as ok
 *     [if] the flag is dismissed [then] no dot, and still restorable ⛔️ gone
 */
import type { AnalysisIssues } from '$lib/rb/job-progress.svelte';
import type { GridClass } from '$lib/rb/grid-quality';
import { gridProvenanceHoverText, type GridProvenanceView } from '$lib/rb/grid-provenance';
import type { BeatgridIssue } from '$lib/rb/library-types';

/** Analysis kinds that have a working detector behind them, today. */
export const DETECTED_ANALYSIS_KINDS = ['beatgrid'] as const;

/** A row's stored beatgrid verdict (GRIDFLAG-02), as the listing sends it.
 * Computed by the server's scan, never here. `message` is the sentence the
 * deck's Beat Sync badge shows for the same grid; null for `ok`. */
export interface GridQualityRow {
	grid_class: GridClass;
	reason: string | null;
	/** The user hid this track's flag. */
	dismissed: boolean;
	message: string | null;
}

/** The row fields this mapping reads. Kept structural so both BrowserRow and
 * a bare rb-meta payload can be passed without a cast. */
export interface AnalysisIssueSource {
	rb_meta?: { beatgrid_issue?: BeatgridIssue | null } | null;
	grid_quality?: GridQualityRow | null;
}

/** A flagged grid's state for the dismiss / restore control. */
export interface GridFlag {
	gridClass: 'suspect' | 'variable_tempo';
	dismissed: boolean;
	message: string;
}

const OK_TITLE = 'Beatgrid: evenly spaced (spacing only, not checked against the audio)';

function _flagMessage(grid: GridQualityRow): string {
	if (grid.message === null) throw new Error(`a ${grid.grid_class} beatgrid verdict carries no message`);
	return grid.message;
}

/** The row's beatgrid flag when its grid is flagged (dismissed or not), else null. */
export function gridFlagFor(row: AnalysisIssueSource): GridFlag | null {
	const grid = row.grid_quality;
	if (grid === null || grid === undefined) return null;
	if (grid.grid_class !== 'suspect' && grid.grid_class !== 'variable_tempo') return null;
	return { gridClass: grid.grid_class, dismissed: grid.dismissed, message: _flagMessage(grid) };
}

function _legacyIssues(row: AnalysisIssueSource): AnalysisIssues {
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

/**
 * Err-column issues for one library row. Empty map = nothing to show.
 *
 * The stored beatgrid verdict (`grid_quality`) is the authority whenever the
 * grid was judged: `suspect` lights a warning, `variable_tempo` a quieter
 * `info`, `ok` and a dismissed flag light nothing. An `unknown` grid falls
 * back to the older field-vs-interval detector when that has a finding, and
 * otherwise shows as `unknown` - never as ok.
 */
export function analysisIssuesFor(row: AnalysisIssueSource): AnalysisIssues {
	const grid = row.grid_quality;
	if (grid === null || grid === undefined) return _legacyIssues(row);
	if (grid.grid_class === 'ok') return {};
	else if (grid.grid_class === 'unknown') {
		const legacy = _legacyIssues(row);
		if (legacy.beatgrid !== undefined) return legacy;
		return { beatgrid: { severity: 'unknown', detail: _flagMessage(grid) } };
	} else if (grid.grid_class === 'suspect' || grid.grid_class === 'variable_tempo') {
		if (grid.dismissed) return {};
		return {
			beatgrid: {
				severity: grid.grid_class === 'suspect' ? 'warning' : 'info',
				detail: _flagMessage(grid)
			}
		};
	}
	const unhandled: never = grid.grid_class;
	throw new Error(`Unhandled: ${unhandled}`);
}

/** How many slots carry a real finding. `unknown` is not one: it says the
 * detector could not look, not that it found something. */
export function flaggedIssueCount(issues: AnalysisIssues): number {
	return Object.values(issues).filter((issue) => issue.severity !== 'unknown').length;
}

/** Hover title for the row's Err cell: what was measured about the beatgrid,
 * with the numbers. undefined = the cell's default title stands. */
export function errColumnTitle(row: AnalysisIssueSource): string | undefined {
	const grid = row.grid_quality;
	if (grid === null || grid === undefined) return undefined;
	if (grid.grid_class === 'ok') return OK_TITLE;
	const message = _flagMessage(grid);
	const flag = gridFlagFor(row);
	return flag !== null && flag.dismissed ? `Beatgrid flag dismissed - ${message}` : message;
}

/** The beatgrid sentences for the row's BPM hover (GRIDFLAG-05, GRIDFLAG-06):
 * the same stored verdict the Err column shows, then which analysis made the
 * grid and its confidence, read lazily on hover (pin f85c5881). A row with no
 * verdict says so and is never worded as ok. */
export function bpmGridHoverText(row: AnalysisIssueSource, provenance: GridProvenanceView): string {
	const verdict = errColumnTitle(row) ?? 'Beatgrid: not checked for this track yet.';
	return `${verdict} ${gridProvenanceHoverText(provenance)}`;
}

/** Sort rank for the Err column (ascending = most worth a look first):
 * uneven grid, variable tempo, dismissed flag, unknown, ok. null (no verdict
 * on the row) sorts last like every other missing value. */
export function gridFlagSortValue(row: AnalysisIssueSource): number | null {
	const grid = row.grid_quality;
	if (grid === null || grid === undefined) return null;
	if (grid.grid_class === 'ok') return 4;
	else if (grid.grid_class === 'unknown') return 3;
	else if (grid.dismissed) return 2;
	else if (grid.grid_class === 'variable_tempo') return 1;
	else if (grid.grid_class === 'suspect') return 0;
	const unhandled: never = grid.grid_class;
	throw new Error(`Unhandled: ${unhandled}`);
}
