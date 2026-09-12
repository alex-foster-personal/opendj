import { strict as assert } from 'node:assert';
import test from 'node:test';

import {
	DEFAULT_KPI_CAPTURE_TIMEOUT_S,
	KPI_CAPTURE_HEADROOM_MS,
	KPI_CAPTURE_MIN_TEST_TIMEOUT_MS,
	kpiCaptureTestTimeoutMs,
	kpiCaptureTimeoutS
} from '../e2e/kpi-capture-timeouts.mjs';

test('if KPI_CAPTURE_TIMEOUT_S is unset then the default budget is used, not NaN', () => {
	assert.equal(kpiCaptureTimeoutS(undefined), DEFAULT_KPI_CAPTURE_TIMEOUT_S);
	assert.equal(kpiCaptureTimeoutS(''), DEFAULT_KPI_CAPTURE_TIMEOUT_S);
});

test('if KPI_CAPTURE_TIMEOUT_S is garbage or non-positive then the default budget is used', () => {
	assert.equal(kpiCaptureTimeoutS('abc'), DEFAULT_KPI_CAPTURE_TIMEOUT_S);
	assert.equal(kpiCaptureTimeoutS('0'), DEFAULT_KPI_CAPTURE_TIMEOUT_S);
	assert.equal(kpiCaptureTimeoutS('-5'), DEFAULT_KPI_CAPTURE_TIMEOUT_S);
});

test('if KPI_CAPTURE_TIMEOUT_S is a positive integer then it is the capture budget', () => {
	assert.equal(kpiCaptureTimeoutS('45'), 45);
	assert.equal(kpiCaptureTimeoutS('300'), 300);
});

test('if --timeout-s equals the old hardcoded 120s then the test timeout still exceeds it', () => {
	// The regression this guards: --timeout-s 120 against a fixed 120_000 test
	// timeout raced, and Playwright tore the context down inside the spec's own
	// error path, producing a crash instead of a clean withhold reason.
	assert.ok(kpiCaptureTestTimeoutMs('120') > 120 * 1000);
});

test('if any --timeout-s is given then the test timeout leads it by the headroom', () => {
	for (const seconds of [1, 30, 90, 120, 600]) {
		const budgetMs = seconds * 1000;
		const testTimeoutMs = kpiCaptureTestTimeoutMs(String(seconds));
		assert.ok(
			testTimeoutMs - budgetMs >= KPI_CAPTURE_HEADROOM_MS,
			`budget ${budgetMs}ms leads test timeout ${testTimeoutMs}ms by too little`
		);
	}
});

test('if --timeout-s is tiny then the test timeout still honors the floor', () => {
	assert.equal(kpiCaptureTestTimeoutMs('1'), KPI_CAPTURE_MIN_TEST_TIMEOUT_MS);
});
