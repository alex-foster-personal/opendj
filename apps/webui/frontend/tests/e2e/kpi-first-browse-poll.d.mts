export declare const FIRST_BROWSE_POLL_MS: number;

export interface FirstBrowseState {
	clicks: number;
	rejected: number;
	accepted: boolean;
}

export interface FirstBrowseObservation {
	rowVisible: boolean;
	ariaSelected: 'true' | 'false' | null;
}

export interface FirstBrowseStep {
	action: 'wait' | 'click' | 'accepted';
	state: FirstBrowseState;
}

export declare function initialFirstBrowseState(): FirstBrowseState;

export declare function nextFirstBrowseStep(
	state: FirstBrowseState,
	observation: FirstBrowseObservation
): FirstBrowseStep;

export declare function firstBrowseTimings(params: {
	clickTimestampsMs: number[];
	openToRowsMs: number | null;
}): {
	nav_start_to_click_ms: number | null;
	rows_paint_to_click_ms: number | null;
};
