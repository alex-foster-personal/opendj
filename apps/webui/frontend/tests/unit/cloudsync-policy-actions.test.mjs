/**
 * CSCLEAR-01..03: the /cloudsync write actions that used to be inert, run
 * against the REAL typed clients (openapi-fetch over the generated paths) with
 * only `fetch` replaced, so each test asserts the HTTP request that actually
 * leaves the page: method, path and body.
 *
 * Regression one-liners:
 * - if picking unset on a stored cell does not send DELETE
 *   /api/v1/cloudsync/policies/{machine_id}/{asset_kind} then broken (CSCLEAR-01)
 * - if a policy cell stops offering 'unset' once a row is stored then broken
 * - if Unpin sends anything but a live (dry_run false) apply that removes
 *   exactly one pin, or reports success when nothing was written, then broken (CSCLEAR-02)
 * - if the grant form posts anything but {owner_email}, or a refusal code
 *   surfaces as raw text instead of a plain sentence, then broken (CSCLEAR-03)
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { bundleSvelteEntry, renderToHtml } from './mount-svelte.mjs';

const API_BASE = 'https://cloudsync-actions.example.test';

let view;
let cloudsync;
let ops;
let ApiError;
let originalFetch;
let calls;

function outcome(overrides = {}) {
	return {
		author_machine_id: 'm1',
		blocking: false,
		measurable: true,
		plan: [],
		violations: [],
		written: true,
		...overrides
	};
}

function json(status, body) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

/** Record every request and answer from `responder(request)`. */
function stubFetch(responder) {
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		const text = await request.clone().text();
		calls.push({
			method: request.method,
			path: new URL(request.url).pathname,
			body: text === '' ? null : JSON.parse(text)
		});
		return responder(request);
	};
}

before(async () => {
	view = await loadTypeScriptModule('src/lib/components/cloudsync/cloudsync-view.ts', {
		viteApiBase: API_BASE
	});
	cloudsync = await loadTypeScriptModule('src/lib/api-cloudsync.ts', { viteApiBase: API_BASE });
	ops = await loadTypeScriptModule('src/lib/api-cloudsync-ops.ts', { viteApiBase: API_BASE });
	ApiError = (await loadTypeScriptModule('src/lib/api/client.ts')).ApiError;
	originalFetch = globalThis.fetch;
});

beforeEach(() => {
	calls = [];
});

after(() => {
	globalThis.fetch = originalFetch;
});

// ----------------------------------------------------------- CSCLEAR-01

test('picking unset on a stored cell sends DELETE for exactly that cell', async () => {
	stubFetch(() => json(200, outcome()));
	const stored = { machine_id: 'm 1', asset_kind: 'stem_bundle', mode: 'pinned', cache_budget_mb: null };
	const decision = view.policyModeChange('m 1', 'stem_bundle', stored, 'unset');
	const result = await view.commitPolicyDecision(decision);

	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, 'DELETE');
	assert.equal(calls[0].path, '/api/v1/cloudsync/policies/m%201/stem_bundle');
	assert.equal(calls[0].body, null);
	assert.equal(result.kind, 'cleared');
	assert.equal(result.machineId, 'm 1');
	assert.equal(result.assetKind, 'stem_bundle');
});

test('a real mode pick still PUTs (the overshoot control: not every pick is a DELETE)', async () => {
	stubFetch(() =>
		json(200, { machine_id: 'm1', asset_kind: 'audio', mode: 'stream', cache_budget_mb: null })
	);
	const decision = view.policyModeChange('m1', 'audio', undefined, 'stream');
	const result = await view.commitPolicyDecision(decision);
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, 'PUT');
	assert.equal(calls[0].path, '/api/v1/cloudsync/policies');
	assert.equal(result.kind, 'saved');
});

test('a clear the gate refuses (409 POLICY_VIOLATION) throws with the blocking reason', async () => {
	stubFetch(() =>
		json(409, {
			detail: {
				code: 'POLICY_VIOLATION',
				message: 'blocked',
				outcome: outcome({
					blocking: true,
					written: false,
					violations: [{ subject: 'm1/audio', message: 'hub must keep audio', blocking: true }]
				})
			}
		})
	);
	const decision = view.policyModeChange('m1', 'audio', { mode: 'pinned' }, 'unset');
	await assert.rejects(
		() => view.commitPolicyDecision(decision),
		(exc) =>
			// Separate bundles hold separate ApiError classes, so read the
			// fields policyErrorMessage reads rather than relying on instanceof.
			exc.code === 'POLICY_VIOLATION' &&
			exc.body.detail.outcome.violations[0].message === 'hub must keep audio'
	);
});

test('a 2xx clear that wrote nothing is not reported as cleared', async () => {
	stubFetch(() => json(200, outcome({ written: false })));
	const decision = view.policyModeChange('m1', 'audio', { mode: 'pinned' }, 'unset');
	await assert.rejects(() => view.commitPolicyDecision(decision), /wrote nothing/);
});

test('the policy cell offers unset with its local-only title on a STORED cell too', async () => {
	const m = await bundleSvelteEntry(
		`export { default as Cell } from '$lib/components/cloudsync/CloudSyncPolicyCell.svelte';`
	);
	const html = await renderToHtml(m.Cell, {
		machineId: 'm1',
		machineName: 'gig laptop',
		assetKind: 'audio',
		kindLabel: 'Audio',
		policy: { machine_id: 'm1', asset_kind: 'audio', mode: 'pinned', cache_budget_mb: null },
		onSaved: () => {},
		onCleared: () => {},
		onError: () => {}
	});
	const unsetOption = html.match(/<option[^>]*value="unset"[^>]*>/);
	assert.ok(unsetOption, `no unset option rendered in: ${html}`);
	assert.match(unsetOption[0], /local-only/);
	assert.doesNotMatch(html, /PARITY-TODO/);
	// The four real modes still render alongside it.
	for (const mode of ['pinned', 'cached', 'stream', 'excluded']) {
		assert.match(html, new RegExp(`<option[^>]*value="${mode}"`));
	}
});

// ----------------------------------------------------------- CSCLEAR-02

test('Unpin sends one live apply removing exactly that pin', async () => {
	stubFetch(() => json(200, outcome()));
	await cloudsync.removePlaylistPin('m1', 'pl-42');
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, 'POST');
	assert.equal(calls[0].path, '/api/v1/cloudsync/policies/apply');
	assert.deepEqual(calls[0].body, {
		changes: {
			defaults: [],
			excluded_tables: [],
			pins: [],
			policies: [],
			removed_pins: [{ machine_id: 'm1', playlist_id: 'pl-42' }],
			removed_policies: []
		},
		dry_run: false
	});
});

test('Unpin fails loudly when the apply wrote nothing or the pin is unknown', async () => {
	stubFetch(() => json(200, outcome({ written: false })));
	await assert.rejects(() => cloudsync.removePlaylistPin('m1', 'pl-42'), /not removed/);
	stubFetch(() => json(404, { detail: { code: 'PIN_NOT_FOUND', message: 'no such pin' } }));
	await assert.rejects(
		() => cloudsync.removePlaylistPin('m1', 'pl-42'),
		(exc) => exc instanceof Error && exc.code === 'PIN_NOT_FOUND'
	);
});

// ----------------------------------------------------------- CSCLEAR-03

test('the grant form posts only the trimmed owner email', async () => {
	const minted = {
		token: 'tok-secret',
		owner_email: 'dj@example.test',
		created_at: '2026-10-09T10:00:00+00:00',
		expires_at: '2026-10-09T10:15:00+00:00'
	};
	stubFetch(() => json(201, minted));
	const decision = view.grantRequest('  dj@example.test ');
	assert.deepEqual(decision, { kind: 'post', body: { owner_email: 'dj@example.test' } });
	const grant = await ops.mintEnrollmentGrant(decision.body);
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, 'POST');
	assert.equal(calls[0].path, '/api/v1/cloudsync/enrollment-grants');
	assert.deepEqual(calls[0].body, { owner_email: 'dj@example.test' });
	assert.deepEqual(grant, minted);
	assert.match(view.grantExpiryTitle(grant), /UTC/);
});

test('an empty or malformed grant email is refused without a request', () => {
	assert.equal(view.grantRequest('   ').kind, 'refuse');
	assert.equal(view.grantRequest('not-an-email').kind, 'refuse');
});

test('each documented grant refusal maps to a plain sentence', () => {
	const cases = {
		CLOUDSYNC_OPS_LOCAL_ONLY: /only be created on the hub machine itself/,
		CLOUDSYNC_NOT_A_HUB: /not a hub/,
		CLOUDSYNC_GRANT_OWNER_UNKNOWN: /has signed in on this hub/,
		CLOUDSYNC_HUB_FLAG_INVALID: /MDT_IS_HUB/
	};
	for (const [code, sentence] of Object.entries(cases)) {
		const msg = view.grantErrorSentence(new ApiError(409, code, `${code}: raw`, null, null));
		assert.match(msg, sentence, code);
		assert.doesNotMatch(msg, new RegExp(code), `${code} must not leak as a raw code`);
	}
	assert.match(view.grantErrorSentence(new Error('boom')), /Could not create the enrollment grant: boom/);
});

test('the enroll hint matches the real CLI flags', () => {
	assert.match(view.ENROLL_COMMAND_HINT, /^python -m apps\.sync_hub enroll /);
	for (const flag of ['--data-dir', '--hub', '--name', '--grant-file -']) {
		assert.ok(view.ENROLL_COMMAND_HINT.includes(flag), flag);
	}
});

test('the Fleet tab renders the grant form with its button and no token before a mint', async () => {
	const m = await bundleSvelteEntry(
		`export { default as Fleet } from '$lib/components/cloudsync/CloudSyncFleetTab.svelte';`
	);
	const html = await renderToHtml(m.Fleet, {});
	assert.match(html, /data-testid="cloudsync-grant-form"/);
	assert.match(html, />\s*Create enrollment grant\s*</);
	assert.match(html, /<input[^>]*type="email"/);
	assert.doesNotMatch(html, /cloudsync-grant-out/);
});
