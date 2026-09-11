/**
 * The machine-condition stamp: what a deck-load row says about the box.
 *
 * Two properties matter more than the happy path here, and both are cases
 * that MUST show an absence rather than a number:
 *
 * 1. NEVER A ZERO. A row written before any reading arrived says
 *    `pressure=unknown` and carries no numeric field at all. `load_avg_1m=0`
 *    on an unsampled box reads exactly like a genuinely idle one, and an
 *    unmeasured condition rendered as a real reading is the defect
 *    .claude/rules/verification.md exists to forbid.
 * 2. THE POLLER STOPS. A hidden tab loads no decks, and a timer the browser
 *    silently throttles to a minute would turn a documented 10 s cadence into
 *    an undocumented one.
 *
 * The FIRST poll is deferred out of the boot request burst (PERF-R6), so
 * every case below except the deferral one hands startMachinePressurePolling
 * an immediate scheduler: their subject is the reading and the listener
 * wiring, not the boot window. The deferral itself gets its own case at the
 * bottom, and its ordering is proven in boot-scheduler.test.mjs.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { immediateBootScheduler, manualBootScheduler } from './fake-boot-scheduler.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://engine.example.test';

let pressure;
let originalFetch;
let originalSetInterval;
let originalClearInterval;
let visibilityHandlers;
let intervals;
let fetched;
let respond;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

/** A document whose visibility the test drives by hand. */
function fakeDocument(initial) {
	return {
		visibilityState: initial,
		addEventListener(type, handler) {
			if (type === 'visibilitychange') visibilityHandlers.push(handler);
		},
		removeEventListener(type, handler) {
			if (type !== 'visibilitychange') return;
			visibilityHandlers = visibilityHandlers.filter((entry) => entry !== handler);
		}
	};
}

function setVisibility(state) {
	globalThis.document.visibilityState = state;
	for (const handler of [...visibilityHandlers]) handler();
}

/** Let every fetch already issued settle before asserting on the cache. */
async function settle() {
	for (let i = 0; i < 5; i++) await Promise.resolve();
}

before(async () => {
	originalFetch = globalThis.fetch;
	originalSetInterval = globalThis.setInterval;
	originalClearInterval = globalThis.clearInterval;
});

beforeEach(async () => {
	visibilityHandlers = [];
	intervals = [];
	fetched = [];
	respond = () => ({ ok: true, json: async () => ({ available: false, reason: 'stub' }) });
	defineGlobal('window', { addEventListener() {}, removeEventListener() {} });
	defineGlobal('document', fakeDocument('visible'));
	defineGlobal('fetch', (url) => {
		fetched.push(url);
		return Promise.resolve(respond());
	});
	defineGlobal('setInterval', (fn, delayMs) => {
		const handle = { fn, delayMs, cleared: false };
		intervals.push(handle);
		return handle;
	});
	defineGlobal('clearInterval', (handle) => {
		if (handle) handle.cleared = true;
	});
	// A fresh module per case: the snapshot cache is module state with a page
	// lifetime in production, so isolating cases by reloading is honest rather
	// than reaching in to reset something production never resets.
	pressure = await loadTypeScriptModule('src/lib/rb/machine-pressure.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	defineGlobal('fetch', originalFetch);
	defineGlobal('setInterval', originalSetInterval);
	defineGlobal('clearInterval', originalClearInterval);
});

test('with no reading ever taken, a row says unknown and carries NO numbers', () => {
	const labels = pressure.pressureLabels(pressure.readMachinePressure(), 1_000);
	assert.deepEqual(labels, { pressure: 'unknown' });
	// Named explicitly: these are the keys a reader would mistake for a real
	// measurement if this module ever defaulted them.
	for (const key of ['load_avg_1m', 'mem_free_mb', 'swap_used_mb', 'pressure_age_ms']) {
		assert.equal(labels[key], undefined, `${key} must be absent, not zero`);
	}
});

test('a real reading becomes numeric labels with an honest age', async () => {
	respond = () => ({
		ok: true,
		json: async () => ({
			available: true,
			load_avg_1m: 5.76,
			mem_free_mb: 67.7,
			swap_used_mb: 6535.4,
			cache_age_ms: 250
		})
	});
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	const snapshot = pressure.readMachinePressure();
	assert.notEqual(snapshot, null);
	const labels = pressure.pressureLabels(snapshot, snapshot.requestedAtMs + 1_000);
	assert.equal(labels.load_avg_1m, '5.76');
	assert.equal(labels.mem_free_mb, '67.7');
	assert.equal(labels.swap_used_mb, '6535.4');
	// 1000 ms since this client received it, plus the 250 ms the sample was
	// already stale server-side. Counting from the response instead would make
	// a slow round trip look fresh.
	assert.equal(labels.pressure_age_ms, '1250');
	assert.equal(labels.pressure, undefined);
	stop();
});

test('an engine that says it cannot measure leaves the cache empty', async () => {
	respond = () => ({
		ok: true,
		json: async () => ({ available: false, reason: 'native machine sampler is not importable' })
	});
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	assert.equal(pressure.readMachinePressure(), null);
	assert.deepEqual(pressure.pressureLabels(pressure.readMachinePressure(), 0), {
		pressure: 'unknown'
	});
	stop();
});

test('a partial reading stamps only the fields the engine could read', async () => {
	respond = () => ({
		ok: true,
		json: async () => ({ available: true, load_avg_1m: 12.5, cache_age_ms: 0 })
	});
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	const snapshot = pressure.readMachinePressure();
	const labels = pressure.pressureLabels(snapshot, snapshot.requestedAtMs);
	assert.equal(labels.load_avg_1m, '12.5');
	assert.equal(labels.mem_free_mb, undefined);
	assert.equal(labels.swap_used_mb, undefined);
	stop();
});

test('an engine that is down leaves the cache empty rather than throwing', async () => {
	defineGlobal('fetch', () => Promise.reject(new Error('connection refused')));
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	assert.equal(pressure.readMachinePressure(), null);
	stop();
});

test('the poll targets the engine base and the documented low-rate cadence', async () => {
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	assert.equal(fetched[0], `${API_BASE}/api/v1/performance/telemetry/pressure`);
	assert.equal(intervals.length, 1);
	assert.equal(intervals[0].delayMs, 10_000);
	stop();
});

test('hiding the page stops the timer; showing it resamples and restarts', async () => {
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	const firstTimer = intervals[0];
	const pollsWhileVisible = fetched.length;

	setVisibility('hidden');
	assert.equal(firstTimer.cleared, true, 'a hidden tab must not keep a timer armed');
	assert.equal(fetched.length, pollsWhileVisible, 'hiding must not issue a poll');

	setVisibility('visible');
	await settle();
	assert.equal(fetched.length, pollsWhileVisible + 1, 'coming back must resample at once');
	assert.equal(intervals.length, 2);
	assert.equal(intervals[1].cleared, false);
	stop();
});

test('teardown clears the timer and removes the visibility listener', async () => {
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	stop();
	assert.equal(intervals[0].cleared, true);
	assert.equal(visibilityHandlers.length, 0);
});

test('a page that starts hidden arms nothing until it is shown', async () => {
	defineGlobal('document', fakeDocument('hidden'));
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	assert.equal(fetched.length, 0);
	assert.equal(intervals.length, 0);
	stop();
});

test('the first poll waits for the boot window instead of joining the burst', async () => {
	// PERF-R6: a deck load at startup competes with this fetch for the
	// six-connection origin, and the poll invokes the sysctl/vm_stat sampler
	// on the exact path the boot scheduler exists to keep quiet.
	// [if the first poll fires at mount then it is back in the burst]
	const manual = manualBootScheduler();
	const stop = pressure.startMachinePressurePolling(manual.scheduler);
	await settle();

	assert.equal(fetched.length, 0, 'no poll may go out during the boot window');
	assert.equal(manual.pending(), 1, 'and it must be queued, never dropped');

	manual.release();
	await settle();
	stop();

	assert.equal(fetched.length, 1);
	assert.equal(fetched[0], `${API_BASE}/api/v1/performance/telemetry/pressure`);
	assert.equal(intervals.length, 1, 'the recurring timer starts after release too');
});

test('a page that starts hidden and is shown before the boot window closes still waits', async () => {
	// A background tab (or one switched away from right after open) still owns
	// the same boot window as a foreground one. The visibility handler must
	// not bypass the scheduler just because the deferral began on a different
	// entrance than the mount-time one above (#1658 review).
	defineGlobal('document', fakeDocument('hidden'));
	const manual = manualBootScheduler();
	const stop = pressure.startMachinePressurePolling(manual.scheduler);
	await settle();
	assert.equal(manual.pending(), 0, 'nothing to queue while still hidden');

	setVisibility('visible');
	await settle();
	assert.equal(fetched.length, 0, 'no poll may go out during the boot window');
	assert.equal(manual.pending(), 1, 'becoming visible queues it, rather than firing it directly');

	manual.release();
	await settle();
	stop();

	assert.equal(fetched.length, 1);
	assert.equal(intervals.length, 1);
});

test('toggling visibility while still queued does not fire the poll twice', async () => {
	const manual = manualBootScheduler();
	const stop = pressure.startMachinePressurePolling(manual.scheduler);
	await settle();
	assert.equal(manual.pending(), 1);

	setVisibility('hidden');
	setVisibility('visible');
	await settle();
	assert.equal(manual.pending(), 1, 'still one queued task, not a second one');
	assert.equal(fetched.length, 0, 'the bypass this guards against would have fired here');

	manual.release();
	await settle();
	stop();

	assert.equal(fetched.length, 1, 'exactly one poll, not two');
});

test('pressureIsElevated is false on a null snapshot', () => {
	assert.equal(pressure.pressureIsElevated(null), false);
});

test('kernel level 1 is not elevated', () => {
	const snapshot = {
		loadAvg1m: 1,
		memFreeMb: null,
		swapUsedMb: null,
		kernelLevel: 1,
		churnScore: null,
		swapRate: null,
		decompRate: null,
		requestedAtMs: 1,
		serverCacheAgeMs: 0
	};
	assert.equal(pressure.pressureIsElevated(snapshot), false);
});

test('kernel level 2 is elevated', () => {
	const snapshot = {
		loadAvg1m: 1,
		memFreeMb: null,
		swapUsedMb: null,
		kernelLevel: 2,
		churnScore: null,
		swapRate: null,
		decompRate: null,
		requestedAtMs: 1,
		serverCacheAgeMs: 0
	};
	assert.equal(pressure.pressureIsElevated(snapshot), true);
});

test('churn 499 is not elevated without kernel', () => {
	const snapshot = {
		loadAvg1m: 1,
		memFreeMb: null,
		swapUsedMb: null,
		kernelLevel: null,
		churnScore: 499,
		swapRate: null,
		decompRate: null,
		requestedAtMs: 1,
		serverCacheAgeMs: 0
	};
	assert.equal(pressure.pressureIsElevated(snapshot), false);
});

test('churn 500 is elevated without kernel', () => {
	const snapshot = {
		loadAvg1m: 1,
		memFreeMb: null,
		swapUsedMb: null,
		kernelLevel: null,
		churnScore: 500,
		swapRate: null,
		decompRate: null,
		requestedAtMs: 1,
		serverCacheAgeMs: 0
	};
	assert.equal(pressure.pressureIsElevated(snapshot), true);
});

test('churn_score is computed from swap and decomp rates', async () => {
	respond = () => ({
		ok: true,
		json: async () => ({
			available: true,
			load_avg_1m: 1,
			swap_rate: 40,
			decomp_rate: 100,
			cache_age_ms: 0
		})
	});
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	const snapshot = pressure.readMachinePressure();
	assert.equal(snapshot.churnScore, 500);
	assert.equal(pressure.pressureIsElevated(snapshot), true);
	stop();
});

test('today engine body without kernel or churn is not elevated', async () => {
	respond = () => ({
		ok: true,
		json: async () => ({
			available: true,
			load_avg_1m: 5.76,
			mem_free_mb: 67.7,
			swap_used_mb: 6535.4,
			cache_age_ms: 250
		})
	});
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	assert.equal(pressure.pressureIsElevated(pressure.readMachinePressure()), false);
	stop();
});

test('kernel_level 0 is a real reading, not elevated', async () => {
	respond = () => ({
		ok: true,
		json: async () => ({
			available: true,
			load_avg_1m: 1,
			kernel_level: 0,
			cache_age_ms: 0
		})
	});
	const stop = pressure.startMachinePressurePolling(immediateBootScheduler());
	await settle();
	const snapshot = pressure.readMachinePressure();
	assert.equal(snapshot.kernelLevel, 0);
	assert.equal(pressure.pressureIsElevated(snapshot), false);
	stop();
});

test('subscribeMachinePressure fires only on accepted stores', () => {
	const seen = [];
	const stop = pressure.subscribeMachinePressure((snapshot) => seen.push(snapshot.requestedAtMs));
	const newer = {
		loadAvg1m: 1,
		memFreeMb: null,
		swapUsedMb: null,
		kernelLevel: null,
		churnScore: null,
		swapRate: null,
		decompRate: null,
		requestedAtMs: 200,
		serverCacheAgeMs: 0
	};
	const older = { ...newer, requestedAtMs: 100 };
	assert.equal(pressure.storeMachinePressure(newer), true);
	assert.equal(pressure.storeMachinePressure(older), false);
	assert.deepEqual(seen, [200]);
	stop();
});

test('a page that goes hidden while the first poll is still queued does not poll when released', async () => {
	// The boot window can outlast the tab's visible spell: the release is on
	// the SCHEDULER's clock, not the page's, so by the time it fires the page
	// may already be hidden again. Firing anyway would defeat the whole point
	// of gating on visibility in the first place (#1658 review).
	const manual = manualBootScheduler();
	const stop = pressure.startMachinePressurePolling(manual.scheduler);
	await settle();
	assert.equal(manual.pending(), 1);

	setVisibility('hidden');
	assert.equal(fetched.length, 0, 'hiding must not itself trigger a poll');

	manual.release();
	await settle();

	assert.equal(fetched.length, 0, 'still hidden when released: no poll may go out');
	assert.equal(intervals.length, 0, 'and no recurring timer may start for a hidden page');

	setVisibility('visible');
	await settle();
	stop();

	assert.equal(fetched.length, 1, 'coming back visible afterwards resamples exactly once');
	assert.equal(intervals.length, 1);
});
