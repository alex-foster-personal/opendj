/**
 * runPerformanceCommandFromUi never drops a failure in silence.
 *
 * The UI boundary used to end in a bare `catch {}` on the promise that "the
 * dispatcher already populated the deck alert and toast". That holds only for
 * failures the dispatcher persisted. A command refused because no command
 * session exists yet (the route starts one after its first async hydration)
 * is never persisted, and neither is a failure that lands after its session
 * was torn down; both vanished. The I/O panel opened on three empty menus
 * with no word of why for exactly this reason.
 *
 * Regression one-liners:
 *   - if a command with no session resolves to anything but { ok: false, reason: 'no_session' } then broken
 *   - if the no-session case logs an error, pushes a toast or rejects then broken
 *   - if a failure the dispatcher did not persist reaches neither console.error nor a toast then broken
 *   - if a failure the dispatcher already toasted is toasted a second time then broken
 *   - if a command that succeeds resolves to anything but { ok: true } then broken
 *   - if a load that suppressed its toast fails without a console.error, or with a toast, then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: 'https://ui-command-result.example.test'
	});
});

/** Record console.error / console.debug for the duration of `body`. */
async function withConsole(body) {
	const original = { error: console.error, debug: console.debug };
	const seen = { error: [], debug: [] };
	console.error = (...args) => seen.error.push(args);
	console.debug = (...args) => seen.debug.push(args);
	try {
		await body(seen);
	} finally {
		console.error = original.error;
		console.debug = original.debug;
	}
	return seen;
}

function hotCueDriver() {
	return {
		stableId: () => 'loaded-track',
		refresh: async () => {},
		hasRbMapping: () => true
	};
}

test('no session yet: a typed no_session result, logged at debug, nothing else', async () => {
	assert.equal(globalThis.window?.musicDjToolsPerformance, undefined);
	const errorBefore = ipc.performanceCommandStatus.last_error;
	let result;
	const seen = await withConsole(async () => {
		result = await ipc.runPerformanceCommandFromUi({ type: 'head_delay_ms', value: 10 });
	});

	assert.equal(result.ok, false);
	assert.equal(result.reason, 'no_session');
	assert.equal(result.error.name, 'ScopedCommandInvalidatedError');
	assert.equal(seen.error.length, 0, 'the expected early-boot case is not an error');
	assert.equal(seen.debug.length, 1);
	assert.match(String(seen.debug[0][0]), /head_delay_ms/);
	assert.equal(ipc.performanceCommandStatus.last_error, errorBefore);
});

test('a command that succeeds resolves { ok: true } and logs nothing', async () => {
	globalThis.window = { localStorage: { getItem: () => null, setItem: () => {} } };
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		let result;
		const seen = await withConsole(async () => {
			result = await ipc.runPerformanceCommandFromUi({ type: 'head_delay_ms', value: 12 });
		});
		assert.deepEqual(result, { ok: true });
		assert.equal(seen.error.length, 0);
		assert.equal(seen.debug.length, 0);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('a failure the dispatcher toasted: one console.error row, exactly one toast', async () => {
	globalThis.window = { localStorage: { getItem: () => null, setItem: () => {} } };
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const toastsBefore = window.musicDjToolsPerformance.toasts().length;
		let result;
		const seen = await withConsole(async () => {
			result = await ipc.runPerformanceCommandFromUi({ type: 'head_delay_ms', value: 501 });
		});
		assert.equal(result.ok, false);
		assert.equal(result.reason, 'failed');
		assert.match(String(result.error.message), /head delay must be a finite number/i);
		assert.equal(seen.error.length, 1, JSON.stringify(seen.error.map((a) => String(a[0]).slice(0, 160))));
		assert.equal(window.musicDjToolsPerformance.toasts().length, toastsBefore + 1);
		assert.match(ipc.performanceCommandStatus.last_error, /head delay must be a finite number/i);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('a failure the dispatcher did NOT persist is still reported: one console.error row plus a toast', async () => {
	// A hot cue clear whose request fails AFTER the session was torn down:
	// the dispatcher's catch skips its report (the session is not current),
	// and the error is not a session invalidation, so nothing else speaks.
	const originalFetch = globalThis.fetch;
	let failRequest;
	globalThis.fetch = () =>
		new Promise((_resolve, reject) => {
			failRequest = reject;
		});
	globalThis.window = { localStorage: { getItem: () => null, setItem: () => {} } };
	const resetDriver = ipc.installPerformanceHotCueDriverForTest(hotCueDriver());
	const uninstall = ipc.installPerformanceBrowserIpc();
	// The frozen IPC object outlives uninstall, and its toast listing asserts
	// no session: the one way to read the toast store from outside the bundle.
	const browserIpc = window.musicDjToolsPerformance;
	const toastCount = () => browserIpc.toasts().length;
	let uninstalled = false;
	try {
		const toastsBefore = toastCount();
		let result;
		const seen = await withConsole(async () => {
			const pending = ipc.runPerformanceCommandFromUi({
				type: 'hot_cue_clear', deck: 1, slot: 'A', revision: 'rev'
			});
			while (failRequest === undefined) await new Promise((resolve) => setImmediate(resolve));
			uninstall();
			uninstalled = true;
			failRequest(new Error('boom after teardown'));
			result = await pending;
		});
		assert.equal(result.ok, false);
		assert.equal(result.reason, 'failed');
		assert.match(String(result.error.message), /boom after teardown/);
		assert.equal(seen.error.length, 1, 'an unpersisted failure must reach console.error');
		assert.equal(toastCount(), toastsBefore + 1, 'and the toast channel');
	} finally {
		if (!uninstalled) uninstall();
		resetDriver();
		delete globalThis.window;
		globalThis.fetch = originalFetch;
	}
});

test('a load that suppressed its toast still reaches console.error, and stays toast-free', async () => {
	globalThis.window = { localStorage: { getItem: () => null, setItem: () => {} } };
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		// Earlier cases in this file leave error toasts whose 5s dismissal timer
		// is still armed. A missing-track load waits on fetch, and in CI that
		// wait outlives the timer, so a raw length compare sees those toasts
		// vanish and reports a toast this load did not raise. Count only toasts
		// this call introduces.
		const toastIdsBefore = new Set(window.musicDjToolsPerformance.toasts().map((toast) => toast.id));
		let result;
		const seen = await withConsole(async () => {
			result = await ipc.runPerformanceCommandFromUi({
				type: 'load', deck: 1, stable_id: 'no-such-track', suppressCommandErrorToast: true
			});
		});
		assert.equal(result.ok, false);
		assert.equal(result.reason, 'failed');
		assert.equal(seen.error.length, 1, JSON.stringify(seen.error.map((a) => String(a[0]).slice(0, 160))));
		assert.match(String(seen.error[0][0]), /\[performance-ipc\] load failed/);
		const introduced = window.musicDjToolsPerformance.toasts().filter((toast) => !toastIdsBefore.has(toast.id));
		assert.deepEqual(introduced.map((toast) => toast.message), []);
		assert.notEqual(ipc.performanceCommandStatus.deck_errors[1], null);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});
