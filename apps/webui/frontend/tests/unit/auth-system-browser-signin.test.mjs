/**
 * Desktop-shell sign-in poll loop and detectSurface-driven navigation.
 */

import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SIGNED_IN_USER = {
	google_sub: 'sub-abc',
	email: 'sub-abc@example.com',
	name: 'Test User',
	avatar_url: null,
	created_at: '2026-01-01T00:00:00+00:00'
};

let originalWindow;

afterEach(() => {
	if (originalWindow !== undefined) {
		globalThis.window = originalWindow;
	}
});

function makeTimerHarness() {
	const intervals = [];
	const timeouts = [];
	return {
		intervals,
		timeouts,
		setInterval(fn, ms) {
			const id = intervals.length + 1;
			intervals.push({ id, fn, ms, kind: 'interval' });
			return id;
		},
		clearInterval(id) {
			const index = intervals.findIndex((row) => row.id === id);
			if (index >= 0) intervals.splice(index, 1);
		},
		setTimeout(fn, ms) {
			const id = timeouts.length + 1;
			timeouts.push({ id, fn, ms, kind: 'timeout' });
			return id;
		},
		clearTimeout(id) {
			const index = timeouts.findIndex((row) => row.id === id);
			if (index >= 0) timeouts.splice(index, 1);
		},
		async fireInterval() {
			for (const row of [...intervals]) {
				await row.fn();
			}
		},
		fireTimeout() {
			for (const row of timeouts) {
				row.fn();
			}
		}
	};
}

function makeDeps({ user = null, timers, detectSurface = () => 'desktop-shell' } = {}) {
	let currentUser = user;
	const refreshCalls = [];
	const openUrlCalls = [];
	const locationAssignments = [];
	return {
		deps: {
			refreshUser: async () => {
				refreshCalls.push(refreshCalls.length + 1);
			},
			getUser: () => currentUser,
			setInterval: timers.setInterval.bind(timers),
			clearInterval: timers.clearInterval.bind(timers),
			setTimeout: timers.setTimeout.bind(timers),
			clearTimeout: timers.clearTimeout.bind(timers),
			detectSurface,
			openUrl: async (url) => {
				openUrlCalls.push(url);
			},
			assignLocation: (url) => {
				locationAssignments.push(url);
			}
		},
		setUser(next) {
			currentUser = next;
		},
		refreshCalls,
		openUrlCalls,
		locationAssignments
	};
}

test('navigateForSignIn browser path assigns location and never opens an external URL', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/system-browser-signin.ts');
	const timers = makeTimerHarness();
	const { deps, openUrlCalls, locationAssignments } = makeDeps({
		timers,
		detectSurface: () => 'browser'
	});
	const url = 'https://accounts.google.com/o/oauth2/v2/auth?state=abc';
	const result = await mod.navigateForSignIn(url, {}, deps);
	assert.equal(result.surface, 'browser');
	assert.deepEqual(locationAssignments, [url]);
	assert.equal(openUrlCalls.length, 0);
});

test('navigateForSignIn desktop-shell path opens URL and does not assign location', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/system-browser-signin.ts');
	const timers = makeTimerHarness();
	const { deps, openUrlCalls, locationAssignments } = makeDeps({
		timers,
		detectSurface: () => 'desktop-shell'
	});
	const url = 'https://accounts.google.com/o/oauth2/v2/auth?state=abc';
	const result = await mod.navigateForSignIn(url, {}, deps);
	assert.equal(result.surface, 'desktop-shell');
	assert.deepEqual(openUrlCalls, [url]);
	assert.equal(locationAssignments.length, 0);
	assert.equal(typeof result.session.cancel, 'function');
});

test('desktop poll resolves success when refreshUser sees a user', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/system-browser-signin.ts');
	const timers = makeTimerHarness();
	const harness = makeDeps({ timers });
	const session = mod.startDesktopShellSignInPoll(harness.deps);
	harness.setUser(SIGNED_IN_USER);
	await timers.fireInterval();
	assert.equal(await session.done, 'success');
	assert.equal(timers.intervals.length, 0);
	assert.equal(timers.timeouts.length, 0);
});

test('desktop poll cancel clears timers and stops further polls', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/system-browser-signin.ts');
	const timers = makeTimerHarness();
	const harness = makeDeps({ timers });
	const session = mod.startDesktopShellSignInPoll(harness.deps);
	session.cancel();
	assert.equal(await session.done, 'cancelled');
	assert.equal(timers.intervals.length, 0);
	assert.equal(timers.timeouts.length, 0);
	await timers.fireInterval();
	assert.equal(harness.refreshCalls.length, 0);
});

test('desktop poll timeout clears timers without another refresh', async () => {
	const mod = await loadTypeScriptModule('src/lib/auth/system-browser-signin.ts');
	const timers = makeTimerHarness();
	const harness = makeDeps({ timers });
	const session = mod.startDesktopShellSignInPoll(harness.deps);
	timers.fireTimeout();
	assert.equal(await session.done, 'timeout');
	assert.equal(timers.intervals.length, 0);
	assert.equal(timers.timeouts.length, 0);
	await timers.fireInterval();
	assert.equal(harness.refreshCalls.length, 0);
});
