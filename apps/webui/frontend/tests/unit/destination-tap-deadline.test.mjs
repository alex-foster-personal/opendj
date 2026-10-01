import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const WATCHDOG = 'src/lib/rb/audio-context-watchdog.ts';
const IO_TIMEOUT = 'src/lib/rb/audio-context-io-timeout.ts';
const TAP = 'tests/e2e/support/destination-audio-tap.ts';

/** Worst-case time the product watchdog may spend before giving up. */
function watchdogRecoveryWindowMs(backoffMs, ioTimeoutMs) {
	return backoffMs.reduce((total, delay) => total + delay, 0) + backoffMs.length * ioTimeoutMs;
}

test('if the destination tap deadline is shorter than the watchdog recovery window then broken', async () => {
	const { CONTEXT_RESUME_BACKOFF_MS } = await loadTypeScriptModule(WATCHDOG);
	const { AUDIO_CONTEXT_IO_TIMEOUT_MS } = await loadTypeScriptModule(IO_TIMEOUT);
	const { DESTINATION_CONTEXT_RUNNING_TIMEOUT_MS } = await loadTypeScriptModule(TAP);
	const windowMs = watchdogRecoveryWindowMs(CONTEXT_RESUME_BACKOFF_MS, AUDIO_CONTEXT_IO_TIMEOUT_MS);
	assert.ok(
		DESTINATION_CONTEXT_RUNNING_TIMEOUT_MS >= windowMs,
		`the tap gives up after ${DESTINATION_CONTEXT_RUNNING_TIMEOUT_MS} ms but the watchdog may ` +
			`still be recovering until ${windowMs} ms, so a recovering context reads as dead`
	);
});

test('if the recovery window arithmetic ignores the per-attempt IO timeout then broken', () => {
	// Control: the window is not just the sum of delays (the value the 5,000 ms deadline undercut).
	assert.equal(watchdogRecoveryWindowMs([0, 150, 400, 1_000, 2_500, 5_000], 2_500), 24_050);
	assert.equal(watchdogRecoveryWindowMs([0], 0), 0);
});
