/**
 * Relaunch on the engine-fatal screen must REACT and must report failure.
 *
 * House rule (the maintainer, Tue 15 Sep 2026, after hitting this on silver): all
 * buttons react to interaction, and show an error or warning if their
 * action fails. The shipped version returned silently with no health port
 * and otherwise fired an opaque POST it never read, so the screen looked
 * identical whether the relaunch worked, was refused, or was never sent.
 *
 * Success here is the PRESENCE of an engine answering afterwards, never
 * the absence of a thrown error: an accepted POST only proves the shell's
 * health server took the request.
 *
 * - if pressing Relaunch with no health port leaves the status empty then
 *   the user sees a dead button -> broken.
 * - if a refused request reports anything other than a failure then the
 *   screen lies -> broken.
 * - if nothing answers at the engine address and the status still reads
 *   success then the check is measuring the request, not the engine ->
 *   broken.
 * - if the engine answers and the result is not ok then a working
 *   relaunch is reported as a failure -> broken.
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {
	RELAUNCH_VERIFY_TIMEOUT_MS,
	parseFatalQuery,
	relaunchEngine,
	relaunchUrl
} from '../../../../desktop/setup/setup.js';

const ENGINE_ORIGIN = 'http://127.0.0.1:8685';
const HEALTH_PORT = 51999;

/** Record every (tone, message) the code pushes at the screen. */
function recorder() {
	const seen = [];
	return {
		seen,
		setStatus: (tone, message) => seen.push({ tone, message })
	};
}

/** A clock the test drives, so no test ever waits 20 real seconds. */
function fakeClock() {
	let t = 0;
	return {
		now: () => t,
		sleep: async (ms) => {
			t += ms;
		}
	};
}

test('no health port: says it cannot relaunch, and why', async () => {
	const status = recorder();
	const result = await relaunchEngine({
		healthPort: null,
		engineOrigin: ENGINE_ORIGIN,
		setStatus: status.setStatus,
		fetchImpl: async () => {
			throw new Error('fetch must not be called without a health port');
		}
	});

	assert.equal(result.ok, false);
	assert.equal(result.reason, 'no-health-port');
	const last = status.seen.at(-1);
	assert.equal(last.tone, 'bad');
	assert.match(last.message, /cannot relaunch/i);
	// The press itself is acknowledged before the verdict, so the button
	// is never silent even for the instant before the check runs.
	assert.equal(status.seen[0].tone, 'working');
});

test('request refused: names the address and the refusal', async () => {
	const status = recorder();
	const result = await relaunchEngine({
		healthPort: HEALTH_PORT,
		engineOrigin: ENGINE_ORIGIN,
		setStatus: status.setStatus,
		fetchImpl: async () => {
			throw new Error('Connection refused');
		}
	});

	assert.equal(result.ok, false);
	assert.equal(result.reason, 'request-refused');
	const last = status.seen.at(-1);
	assert.equal(last.tone, 'bad');
	assert.ok(last.message.includes(relaunchUrl(HEALTH_PORT)));
	assert.match(last.message, /Connection refused/);
});

test('accepted but the engine never answers: reported as a failure', async () => {
	const status = recorder();
	const clock = fakeClock();
	let probes = 0;
	const result = await relaunchEngine({
		healthPort: HEALTH_PORT,
		engineOrigin: ENGINE_ORIGIN,
		setStatus: status.setStatus,
		fetchImpl: async () => ({ type: 'opaque' }),
		probe: async () => {
			probes += 1;
			return { reachable: false, url: `${ENGINE_ORIGIN}/api/v1/health`, detail: 'no response' };
		},
		sleep: clock.sleep,
		now: clock.now,
		timeoutMs: 2000,
		pollIntervalMs: 500
	});

	assert.equal(result.ok, false);
	assert.equal(result.reason, 'engine-absent');
	assert.ok(probes > 1, 'must keep checking until the deadline, not once');
	const last = status.seen.at(-1);
	assert.equal(last.tone, 'bad');
	assert.match(last.message, /did not answer/i);
});

test('engine answers after the request: reported as success', async () => {
	const status = recorder();
	const clock = fakeClock();
	let probes = 0;
	const result = await relaunchEngine({
		healthPort: HEALTH_PORT,
		engineOrigin: ENGINE_ORIGIN,
		setStatus: status.setStatus,
		fetchImpl: async () => ({ type: 'opaque' }),
		probe: async () => {
			probes += 1;
			return probes >= 3
				? { reachable: true, url: `${ENGINE_ORIGIN}/api/v1/health`, detail: 'connection accepted' }
				: { reachable: false, url: `${ENGINE_ORIGIN}/api/v1/health`, detail: 'no response' };
		},
		sleep: clock.sleep,
		now: clock.now,
		timeoutMs: RELAUNCH_VERIFY_TIMEOUT_MS,
		pollIntervalMs: 500
	});

	assert.equal(result.ok, true);
	const last = status.seen.at(-1);
	assert.equal(last.tone, 'good');
	assert.ok(last.message.includes(ENGINE_ORIGIN));
});

test('control: an accepted POST alone is NOT success', async () => {
	// The defect this fix exists for. If a future edit reports ok as soon
	// as the POST resolves, this test goes red and the four above stay
	// green, because none of them can tell those two apart on its own.
	const status = recorder();
	const clock = fakeClock();
	const result = await relaunchEngine({
		healthPort: HEALTH_PORT,
		engineOrigin: ENGINE_ORIGIN,
		setStatus: status.setStatus,
		fetchImpl: async () => ({ type: 'opaque' }),
		probe: async () => ({
			reachable: false,
			url: `${ENGINE_ORIGIN}/api/v1/health`,
			detail: 'no response'
		}),
		sleep: clock.sleep,
		now: clock.now,
		timeoutMs: 1000,
		pollIntervalMs: 500
	});
	assert.equal(result.ok, false, 'a taken request is not a restarted engine');
});

// - if the health port only travels in a JS global set BEFORE navigating to
//   the fatal screen then the new document never sees it and Relaunch always
//   says "never told which port" (Thu 1 Oct 2026: it had never worked once,
//   in the Preview or the plain app) -> broken.
test('the fatal screen reads the health port from its own URL', () => {
	delete globalThis.__OPENDJ_ENGINE_SUPERVISOR__;
	const fatal = parseFatalQuery('?fatal=1&exit=1&pid=4242&port=8685&health=51999');
	assert.equal(fatal.health_port, 51999);
	assert.equal(fatal.lock_port, 8685);
});

test('control: no health in the URL and no injected global is null, not a port', () => {
	delete globalThis.__OPENDJ_ENGINE_SUPERVISOR__;
	const fatal = parseFatalQuery('?fatal=1&exit=1&pid=4242&port=8685');
	assert.equal(fatal.health_port, null);
});

test('an injected global still works when the URL carries no health port (Electron preload)', () => {
	globalThis.__OPENDJ_ENGINE_SUPERVISOR__ = { health_port: 52001 };
	try {
		assert.equal(parseFatalQuery('?fatal=1&exit=1&pid=1&port=2').health_port, 52001);
	} finally {
		delete globalThis.__OPENDJ_ENGINE_SUPERVISOR__;
	}
});
