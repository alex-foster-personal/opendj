import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const WATCHDOG = 'src/lib/rb/audio-context-watchdog.ts';
const IO_TIMEOUT = 'src/lib/rb/audio-context-io-timeout.ts';
const MASTER_READY = 'tests/e2e/support/preview-cue-master-ready.ts';

/** Worst-case time the product watchdog may spend on ONE recovery pass. */
function watchdogRecoveryWindowMs(backoffMs, ioTimeoutMs) {
	return backoffMs.reduce((total, delay) => total + delay, 0) + backoffMs.length * ioTimeoutMs;
}

test('if preview-cue-library waits less than two watchdog recovery windows for the deck to go playing/master/moving then broken', async () => {
	const { CONTEXT_RESUME_BACKOFF_MS } = await loadTypeScriptModule(WATCHDOG);
	const { AUDIO_CONTEXT_IO_TIMEOUT_MS } = await loadTypeScriptModule(IO_TIMEOUT);
	const { MASTER_READY_TIMEOUT_MS, watchdogRecoveryWindowMs: liveWindowFn } =
		await loadTypeScriptModule(MASTER_READY);

	const oneWindowMs = watchdogRecoveryWindowMs(CONTEXT_RESUME_BACKOFF_MS, AUDIO_CONTEXT_IO_TIMEOUT_MS);
	assert.equal(
		liveWindowFn(),
		oneWindowMs,
		'preview-cue-master-ready.ts drifted from the live watchdog constants'
	);

	const requiredMs = 2 * oneWindowMs;
	assert.ok(
		MASTER_READY_TIMEOUT_MS >= requiredMs,
		`preview-cue-library's deadline is ${MASTER_READY_TIMEOUT_MS}ms but a single transient hiccup ` +
			`may cost up to ${requiredMs}ms (two re-armed watchdog recovery windows), so a context ` +
			`still legitimately recovering reads as dead`
	);
});

test('if the recovery window arithmetic ignores the per-attempt IO timeout then broken', () => {
	// Control: the window is not just the sum of delays (the value a
	// too-short deadline would undercut).
	assert.equal(watchdogRecoveryWindowMs([0, 150, 400, 1_000, 2_500, 5_000], 2_500), 24_050);
	assert.equal(watchdogRecoveryWindowMs([0], 0), 0);
});

test('if MASTER_READY_TIMEOUT_MS collapses to the bare one-window value then broken', async () => {
	// Control: a regression that reverts to "one window, no re-arm margin"
	// must fail this test, not just the first one above (belt and suspenders
	// - the first test already computes `requiredMs` from live constants, so
	// this pins the concrete literal too in case that computation itself is
	// ever weakened).
	const { CONTEXT_RESUME_BACKOFF_MS } = await loadTypeScriptModule(WATCHDOG);
	const { AUDIO_CONTEXT_IO_TIMEOUT_MS } = await loadTypeScriptModule(IO_TIMEOUT);
	const { MASTER_READY_TIMEOUT_MS } = await loadTypeScriptModule(MASTER_READY);
	const oneWindowMs = watchdogRecoveryWindowMs(CONTEXT_RESUME_BACKOFF_MS, AUDIO_CONTEXT_IO_TIMEOUT_MS);
	assert.ok(
		MASTER_READY_TIMEOUT_MS > oneWindowMs,
		'the deadline must exceed a single recovery window, not just equal it'
	);
});
