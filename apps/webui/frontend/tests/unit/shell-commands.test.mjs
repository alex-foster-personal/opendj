/**
 * Shell command poll: desktop-shell only, apply-update dispatch (issue #2924).
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FAKE_UPDATE_CHANNEL = fileURLToPath(
	new URL('./fake-shell-apply-update.mjs', import.meta.url)
);
const FAKE_REFUSED = fileURLToPath(new URL('./fake-shell-apply-refused.mjs', import.meta.url));

let shellCommands;
let fetchCalls;

function installGlobals({ shell = false } = {}) {
	globalThis.__shellApplyCalls = 0;
	fetchCalls = [];
	const win = {
		location: { pathname: '/' },
		OPENDJ_ENGINE_ORIGIN: shell ? 'http://127.0.0.1:8685' : undefined
	};
	Object.defineProperty(globalThis, 'window', {
		value: win,
		configurable: true,
		writable: true,
		enumerable: true
	});
	let nextPolls = 0;
	globalThis.fetch = async (url, init) => {
		fetchCalls.push({ url, init });
		if (url.endsWith('/api/v1/commands/next?consumer=shell')) {
			nextPolls += 1;
			if (nextPolls > 1) {
				return { ok: true, json: async () => null };
			}
			return {
				ok: true,
				json: async () => ({
					id: 'cmd-apply-1',
					kind: 'shell',
					command: { type: 'apply-update', available_version: '0.1.1' }
				})
			};
		}
		if (url.endsWith('/api/v1/commands/cmd-apply-1/result')) {
			return { ok: true, json: async () => ({ accepted: true }) };
		}
		throw new Error(`unexpected fetch ${url}`);
	};
}

before(async () => {
	shellCommands = await loadTypeScriptModule('src/lib/rb/shell-commands.ts', {
		alias: { '$lib/rb/update-channel': FAKE_UPDATE_CHANNEL }
	});
});

afterEach(() => {
	delete globalThis.window;
	delete globalThis.fetch;
	delete globalThis.__shellApplyCalls;
});

test('installShellCommandPoll is a no-op in browser tabs', () => {
	installGlobals({ shell: false });
	const teardown = shellCommands.installShellCommandPoll();
	teardown();
	assert.equal(fetchCalls.length, 0);
});

test('desktop shell polls apply-update, invokes applyUpdate, and posts result', async () => {
	installGlobals({ shell: true });
	const teardown = shellCommands.installShellCommandPoll();
	await new Promise((resolve) => setTimeout(resolve, 1100));
	teardown();
	assert.ok(
		fetchCalls.some((call) => call.url.endsWith('/api/v1/commands/next?consumer=shell'))
	);
	assert.equal(globalThis.__shellApplyCalls, 1);
	const resultPost = fetchCalls.find((call) =>
		call.url.endsWith('/api/v1/commands/cmd-apply-1/result')
	);
	assert.ok(resultPost);
	assert.equal(resultPost.init?.method, 'POST');
	assert.deepEqual(JSON.parse(resultPost.init?.body), {
		status: 'succeeded',
		outcome: 'installed'
	});
});

test('applyUpdate refused posts failed result with error text', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/shell-commands.ts', {
		alias: { '$lib/rb/update-channel': FAKE_REFUSED }
	});
	const body = await mod.dispatchShellCommand({
		id: 'x',
		kind: 'shell',
		command: { type: 'apply-update', available_version: '0.1.1' }
	});
	assert.deepEqual(body, {
		status: 'failed',
		outcome: 'refused',
		error: 'signature mismatch'
	});
});
