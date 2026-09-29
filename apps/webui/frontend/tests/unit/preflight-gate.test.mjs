/**
 * PREFLIGHT-01's boot gate store (issue #771): the gate holds on a fail
 * response, clears on a pass response, "Re-check" and "Re-request
 * permissions" both re-issue the one real GET, and a network failure holds
 * the gate rather than crashing or clearing it.
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://preflight-gate.example.test';

let preflightStore;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

function failCheckRow(overrides = {}) {
	return {
		id: 'audio-access',
		label: 'Audio access',
		status: 'fail',
		detail: '/tmp/moved-away.mp3: [Errno 2] No such file or directory',
		remediation: 'Clicking Re-request permissions attempts the read again.',
		...overrides
	};
}

function passCheckRow(overrides = {}) {
	return {
		id: 'engine-alive',
		label: 'Engine alive',
		status: 'pass',
		detail: 'the endpoint answered',
		remediation: null,
		...overrides
	};
}

before(async () => {
	preflightStore = await loadTypeScriptModule('src/lib/preflight/preflight.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

afterEach(() => {
	preflightStore._resetPreflightForTests();
});

test('starts unknown and uncleared before any response has arrived', () => {
	assert.equal(preflightStore.preflightGate.status, 'unknown');
	assert.equal(preflightStore.preflightGate.cleared, false);
	assert.deepEqual(preflightStore.preflightGate.checks, []);
});

test('a fail response holds the gate and carries the real check rows', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ status: 'fail', checks: [passCheckRow(), failCheckRow()] });

	await preflightStore.checkPreflight();

	assert.equal(preflightStore.preflightGate.status, 'fail');
	assert.equal(preflightStore.preflightGate.cleared, false);
	assert.equal(preflightStore.preflightGate.checks.length, 2);
	assert.equal(preflightStore.preflightGate.checks[1].id, 'audio-access');
	assert.equal(preflightStore.preflightGate.checks[1].status, 'fail');
});

test('an all-pass response clears the gate', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ status: 'pass', checks: [passCheckRow(), passCheckRow({ id: 'state-db' })] });

	await preflightStore.checkPreflight();

	assert.equal(preflightStore.preflightGate.status, 'pass');
	assert.equal(preflightStore.preflightGate.cleared, true);
});

// REQ: PREFLIGHT-01
test('re-check and re-request permissions both re-issue the same real GET', async () => {
	let calls = 0;
	globalThis.fetch = async (input) => {
		calls += 1;
		assert.equal(input.method, 'GET');
		assert.equal(input.url, `${API_BASE}/api/v1/preflight`);
		return calls === 1
			? jsonResponse({ status: 'fail', checks: [failCheckRow()] })
			: jsonResponse({ status: 'pass', checks: [passCheckRow()] });
	};

	await preflightStore.checkPreflight();
	assert.equal(preflightStore.preflightGate.cleared, false);

	// requestPermissions is deliberately the identical call, not a second
	// mutating endpoint: the audio-access check performs the real gated
	// read every time it runs.
	assert.equal(preflightStore.requestPermissions, preflightStore.checkPreflight);
	await preflightStore.requestPermissions();

	assert.equal(calls, 2);
	assert.equal(preflightStore.preflightGate.cleared, true);
});

test('consecutive fail polls escalate needsActionCopy after the threshold', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ status: 'fail', checks: [failCheckRow()] });

	for (let i = 0; i < 2; i += 1) {
		await preflightStore.checkPreflight();
		assert.equal(preflightStore.preflightGate.needsActionCopy, false);
	}
	await preflightStore.checkPreflight();
	assert.equal(preflightStore.preflightGate.consecutiveFailPolls, 3);
	assert.equal(preflightStore.preflightGate.needsActionCopy, true);

	globalThis.fetch = async () =>
		jsonResponse({ status: 'pass', checks: [passCheckRow()] });
	await preflightStore.checkPreflight();
	assert.equal(preflightStore.preflightGate.consecutiveFailPolls, 0);
	assert.equal(preflightStore.preflightGate.needsActionCopy, false);
});

test('a network failure holds the gate and surfaces the real error, never crashes', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed: ECONNREFUSED');
	};

	await preflightStore.checkPreflight();

	assert.equal(preflightStore.preflightGate.cleared, false);
	assert.match(preflightStore.preflightGate.error, /ECONNREFUSED/);
});
