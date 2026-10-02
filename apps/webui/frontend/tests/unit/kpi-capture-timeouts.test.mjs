import { strict as assert } from 'node:assert';
import test from 'node:test';

import {
	KPI_CAPTURE_HEADROOM_MS,
	KPI_CAPTURE_MAX_TIMEOUT_S,
	KPI_CAPTURE_MIN_TEST_TIMEOUT_MS,
	kpiCaptureTestTimeoutMs,
	kpiCaptureTimeoutS
} from '../e2e/kpi-capture-timeouts.mjs';

test('if KPI_CAPTURE_TIMEOUT_S is unset then it is refused, never defaulted', () => {
	assert.throws(() => kpiCaptureTimeoutS(undefined), /required/);
	assert.throws(() => kpiCaptureTimeoutS(''), /required/);
	assert.throws(() => kpiCaptureTimeoutS('   '), /required/);
});

test('if KPI_CAPTURE_TIMEOUT_S is malformed then it is refused naming the value', () => {
	assert.throws(() => kpiCaptureTimeoutS('abc'), /positive integer/);
	// parseInt would have read these as 90, 1 and 0 respectively.
	assert.throws(() => kpiCaptureTimeoutS('90s'), /positive integer/);
	assert.throws(() => kpiCaptureTimeoutS('1.5'), /positive integer/);
	assert.throws(() => kpiCaptureTimeoutS('0x10'), /positive integer/);
});

test('if KPI_CAPTURE_TIMEOUT_S is non-positive then it is refused', () => {
	assert.throws(() => kpiCaptureTimeoutS('0'), /greater than zero|positive integer/);
	assert.throws(() => kpiCaptureTimeoutS('-5'), /positive integer/);
});

test('if KPI_CAPTURE_TIMEOUT_S is a positive integer then it is the capture budget', () => {
	assert.equal(kpiCaptureTimeoutS('45'), 45);
	assert.equal(kpiCaptureTimeoutS('300'), 300);
	assert.equal(kpiCaptureTimeoutS(' 90 '), 90);
});

// REQ: PERF-CAPTURE-03
test('if --timeout-s equals the old hardcoded 120s then the test timeout still exceeds it', () => {
	// The regression this guards: --timeout-s 120 against a fixed 120_000 test
	// timeout raced, and Playwright tore the context down inside the spec's own
	// error path, producing a crash instead of a clean withhold reason.
	assert.ok(kpiCaptureTestTimeoutMs('120') > 120 * 1000);
});

// REQ: PERF-CAPTURE-03
test('if any --timeout-s is given then the test timeout leads it by the headroom', () => {
	for (const seconds of [1, 30, 90, 120, 600, KPI_CAPTURE_MAX_TIMEOUT_S]) {
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

test('if the test timeout is derived from a bad value then it refuses too', () => {
	assert.throws(() => kpiCaptureTestTimeoutMs('abc'), /positive integer/);
	assert.throws(() => kpiCaptureTestTimeoutMs(undefined), /required/);
});

test('if KPI_CAPTURE_TIMEOUT_S is a digit run that parses to Infinity then it is refused', () => {
	// The regex accepts this and parseInt returns Infinity, which survives a
	// `<= 0` guard and reaches Playwright as an unbounded timeout.
	const huge = '9'.repeat(400);
	assert.equal(Number.parseInt(huge, 10), Infinity);
	assert.throws(() => kpiCaptureTimeoutS(huge), /representable/);
});

test('if KPI_CAPTURE_TIMEOUT_S exceeds the ceiling then it is refused', () => {
	assert.throws(() => kpiCaptureTimeoutS(String(KPI_CAPTURE_MAX_TIMEOUT_S + 1)), /at most/);
	assert.equal(kpiCaptureTimeoutS(String(KPI_CAPTURE_MAX_TIMEOUT_S)), KPI_CAPTURE_MAX_TIMEOUT_S);
});

test('if a value is accepted then the derived test timeout is always finite', () => {
	for (const seconds of [1, 90, KPI_CAPTURE_MAX_TIMEOUT_S]) {
		assert.ok(Number.isFinite(kpiCaptureTestTimeoutMs(String(seconds))));
	}
});
