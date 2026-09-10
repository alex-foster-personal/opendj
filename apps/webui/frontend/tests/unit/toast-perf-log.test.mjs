import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// pushToast is the app's only user-facing failure channel (107 call sites
// incl. AutoPlay) and it self-dismisses after TOAST_DEFAULT_MS with no other
// trace by default -- which is what turned "why didn't AutoPlay fire" into
// live backend log archaeology on Mon 17 Aug 2026 instead of a lookup.
// pushToast now forwards every toast through perf-event-log's durable ring
// (console + localStorage), so this must never regress silently:
// - if pushToast stops recording via console then a toast is back to purely
//   ephemeral and the next incident is unrecoverable again -- broken
// - if an error-kind toast logs at console.info/warn instead of
//   console.error then it can't be grep-filtered as a real failure -- broken
// - if an info-kind toast logs at console.error then routine notices read
//   as failures and drown out real ones -- broken

/** Bundling isolates modules per call, so intercept via the shared global
 * `console` the bundled code looks up at call time rather than a fake window. */
function _captureConsole() {
	const calls = { info: [], warn: [], error: [] };
	const real = { info: console.info, warn: console.warn, error: console.error };
	console.info = (...args) => calls.info.push(args.join(' '));
	console.warn = (...args) => calls.warn.push(args.join(' '));
	console.error = (...args) => calls.error.push(args.join(' '));
	return {
		calls,
		restore: () => {
			console.info = real.info;
			console.warn = real.warn;
			console.error = real.error;
		}
	};
}

// SCOPE NOTE (Thu 10 Sep 2026). These assert the SEVERITY ROUTING OF THE TOAST
// LINE, and are deliberately scoped to `[perf-event] toast-` lines rather than
// to total console counts. Once the escalation allowlist was removed from
// perf-event-log.ts, `toast-error` began escalating like every other
// error-severity row -- which is the point, since a screen full of error toasts
// that never left the browser is what made the Thu 10 Sep audio outage
// undiagnosable. In a unit test no escalator is wired, so _escalate emits its
// documented one-per-session "no escalator wired" console.warn. Counting ALL
// warns made an unrelated subsystem's diagnostic fail a test about toast
// severity; counting the toast lines tests what the docstring above says.
function _toastLines(list) {
	return list.filter((line) => line.includes('[perf-event] toast-'));
}

test('an error toast is durably logged at console.error, not merely shown', async () => {
	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts');
	const capture = _captureConsole();
	try {
		stores.pushToast('auto-play: no free/stopped follower deck', 'error');
	} finally {
		capture.restore();
	}
	assert.equal(_toastLines(capture.calls.warn).length, 0);
	assert.equal(_toastLines(capture.calls.info).length, 0);
	assert.equal(_toastLines(capture.calls.error).length, 1);
	// The id between the kind and the colon is load-bearing, not incidental
	// formatting: it is the string the toast also prints and copies, and it is
	// what makes this line findable from a pasted report.
	assert.match(
		capture.calls.error[0],
		/\[perf-event] toast-error id=t-[a-z0-9]+-\d+:.*no free\/stopped follower deck/
	);
});

test('an info toast is durably logged at console.info, not console.error', async () => {
	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts');
	const capture = _captureConsole();
	try {
		stores.pushToast('auto-play: deck 2 Beat Sync skipped', 'info');
	} finally {
		capture.restore();
	}
	assert.equal(_toastLines(capture.calls.error).length, 0);
	assert.equal(_toastLines(capture.calls.warn).length, 0);
	assert.equal(_toastLines(capture.calls.info).length, 1);
	assert.match(
		capture.calls.info[0],
		/\[perf-event] toast-info id=t-[a-z0-9]+-\d+:.*Beat Sync skipped/
	);
});
