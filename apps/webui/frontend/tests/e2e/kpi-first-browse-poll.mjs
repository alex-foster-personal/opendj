/**
 * Pure poll reducer for the S9/S11 first-browse KPI capture (issue #10240).
 */

export const FIRST_BROWSE_POLL_MS = 100;

/** @typedef {{ clicks: number, rejected: number, accepted: boolean }} FirstBrowseState */
/** @typedef {{ rowVisible: boolean, ariaSelected: 'true' | 'false' | null }} FirstBrowseObservation */
/** @typedef {{ action: 'wait' | 'click' | 'accepted', state: FirstBrowseState }} FirstBrowseStep */

/**
 * @returns {FirstBrowseState}
 */
export function initialFirstBrowseState() {
	return { clicks: 0, rejected: 0, accepted: false };
}

/**
 * @param {FirstBrowseState} state
 * @param {FirstBrowseObservation} observation
 * @returns {FirstBrowseStep}
 */
export function nextFirstBrowseStep(state, observation) {
	if (!observation.rowVisible) {
		return { action: 'wait', state };
	}
	if (observation.ariaSelected === 'true') {
		if (state.clicks > 0) {
			return { action: 'accepted', state: { ...state, accepted: true } };
		}
		return { action: 'wait', state };
	}
	if (state.clicks > 0) {
		return {
			action: 'click',
			state: { ...state, rejected: state.rejected + 1, clicks: state.clicks + 1 }
		};
	}
	return { action: 'click', state: { ...state, clicks: 1 } };
}

/**
 * @param {{ clickTimestampsMs: number[], openToRowsMs: number | null }} params
 * @returns {{ nav_start_to_click_ms: number | null, rows_paint_to_click_ms: number | null }}
 */
export function firstBrowseTimings({ clickTimestampsMs, openToRowsMs }) {
	if (!Array.isArray(clickTimestampsMs) || clickTimestampsMs.length === 0) {
		return { nav_start_to_click_ms: null, rows_paint_to_click_ms: null };
	}
	const nav_start_to_click_ms = clickTimestampsMs[clickTimestampsMs.length - 1];
	if (
		openToRowsMs === null ||
		typeof openToRowsMs !== 'number' ||
		!Number.isFinite(openToRowsMs) ||
		typeof nav_start_to_click_ms !== 'number' ||
		!Number.isFinite(nav_start_to_click_ms)
	) {
		return { nav_start_to_click_ms, rows_paint_to_click_ms: null };
	}
	const delta = nav_start_to_click_ms - openToRowsMs;
	if (!Number.isFinite(delta) || delta < 0) {
		return { nav_start_to_click_ms, rows_paint_to_click_ms: null };
	}
	return { nav_start_to_click_ms, rows_paint_to_click_ms: delta };
}
