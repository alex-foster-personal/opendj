import { strict as assert } from 'node:assert';
import test from 'node:test';

import {
	FIRST_BROWSE_POLL_MS,
	firstBrowseTimings,
	initialFirstBrowseState,
	nextFirstBrowseStep
} from '../e2e/kpi-first-browse-poll.mjs';

test('if the row stays aria-selected false after a click then polling continues and rejected increments', () => {
	let state = initialFirstBrowseState();
	const falseObs = { rowVisible: true, ariaSelected: /** @type {const} */ ('false') };
	({ state } = nextFirstBrowseStep(state, falseObs));
	assert.equal(state.clicks, 1);
	assert.equal(state.rejected, 0);
	({ state } = nextFirstBrowseStep(state, falseObs));
	assert.equal(state.clicks, 2);
	assert.equal(state.rejected, 1);
	({ state } = nextFirstBrowseStep(state, falseObs));
	assert.equal(state.clicks, 3);
	assert.equal(state.rejected, 2);
	const accepted = nextFirstBrowseStep(state, { rowVisible: true, ariaSelected: 'true' });
	assert.equal(accepted.action, 'accepted');
	assert.equal(accepted.state.clicks, 3);
	assert.equal(accepted.state.rejected, 2);
});

test('if the row is already selected before any click then the step is wait', () => {
	const step = nextFirstBrowseStep(initialFirstBrowseState(), {
		rowVisible: true,
		ariaSelected: 'true'
	});
	assert.equal(step.action, 'wait');
});

test('if the row is not visible then the step is wait', () => {
	const step = nextFirstBrowseStep(initialFirstBrowseState(), {
		rowVisible: false,
		ariaSelected: null
	});
	assert.equal(step.action, 'wait');
});

test('firstBrowseTimings maps accepted click and open-to-rows span', () => {
	const timings = firstBrowseTimings({ clickTimestampsMs: [3000, 4200], openToRowsMs: 3300 });
	assert.equal(timings.nav_start_to_click_ms, 4200);
	assert.equal(timings.rows_paint_to_click_ms, 900);
});

test('firstBrowseTimings returns null paint delta when the span is missing', () => {
	const timings = firstBrowseTimings({ clickTimestampsMs: [4200], openToRowsMs: null });
	assert.equal(timings.nav_start_to_click_ms, 4200);
	assert.equal(timings.rows_paint_to_click_ms, null);
});

test('firstBrowseTimings returns null paint delta on negative skew', () => {
	const timings = firstBrowseTimings({ clickTimestampsMs: [1000], openToRowsMs: 2000 });
	assert.equal(timings.nav_start_to_click_ms, 1000);
	assert.equal(timings.rows_paint_to_click_ms, null);
});

test('FIRST_BROWSE_POLL_MS is 100ms', () => {
	assert.equal(FIRST_BROWSE_POLL_MS, 100);
});
