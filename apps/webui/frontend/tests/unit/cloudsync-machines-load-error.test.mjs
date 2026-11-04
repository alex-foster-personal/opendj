/**
 * The /cloudsync page's load error names the server's reason, not "Internal
 * Server Error" (silver, Tue 3 Nov 2026: the configured machine name belonged
 * to a stale row under another machine id). The page's toast and banner print
 * `error.message` from `listMachines()`; this asserts that message is the
 * route's `detail.message` for the 409 it now answers.
 *
 * Regression one-liner:
 * - if listMachines() on a 409 CLOUDSYNC_MACHINE_NAME_CONFLICT surfaces anything
 *   but detail.message as the error message then broken
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://cloudsync-machines.example.test';
const REASON =
	"cannot rename this machine (b...) to 'silver-preview': that name already belongs to machine a...";

let cloudsync;
let originalFetch;

before(async () => {
	cloudsync = await loadTypeScriptModule('src/lib/api-cloudsync.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('a machine-name conflict surfaces the server reason as the error message', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({ detail: { code: 'CLOUDSYNC_MACHINE_NAME_CONFLICT', message: REASON } }),
			{ status: 409, statusText: 'Conflict', headers: { 'content-type': 'application/json' } }
		);
	const caught = await cloudsync.listMachines().then(
		() => null,
		(error) => error
	);
	assert.ok(caught instanceof Error, 'listMachines must reject');
	assert.equal(caught.message, REASON);
	assert.equal(caught.code, 'CLOUDSYNC_MACHINE_NAME_CONFLICT');
	assert.equal(caught.status, 409);
});
