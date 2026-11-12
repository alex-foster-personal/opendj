import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://auto-rec.example.test';
let autoRec;
let originalFetch;
let startPosts;

before(async () => {
	originalFetch = globalThis.fetch;
	autoRec = await loadTypeScriptModule('src/lib/sets/auto-rec.ts', { viteApiBase: API_BASE });
});

after(() => {
	globalThis.fetch = originalFetch;
});

function installFetch({ active = false } = {}) {
	startPosts = 0;
	globalThis.fetch = async (request) => {
		const url = request.url;
		if (url.endsWith('/api/sets/recorder/remembered-input')) {
			return new Response(JSON.stringify({ remembered: { kind: 'master', name: null } }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		if (url.endsWith('/api/sets/recorder') && request.method === 'GET') {
			return new Response(
				JSON.stringify({
					active,
					session_id: active ? '2026-11-10T08-00-00' : null,
					pid: 1,
					owned: true,
					recoverable: false,
					capture: active ? 'recording' : 'none',
					capture_source: 'master'
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.endsWith('/api/sets/recorder/start')) {
			startPosts += 1;
			return new Response(
				JSON.stringify({
					active: true,
					session_id: '2026-11-10T08-00-00',
					pid: 1,
					owned: true,
					recoverable: false,
					capture: 'recording',
					capture_source: 'none'
				}),
				{ status: 201, headers: { 'content-type': 'application/json' } }
			);
		}
		throw new Error(`unexpected fetch ${request.method} ${url}`);
	};
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test('first play starts REC when auto-REC is on', async () => {
	autoRec.resetSetAutoRecSession();
	autoRec.setSetAutoRecEnabledForTest(true);
	installFetch({ active: false });
	autoRec.maybeStartAutoRecOnFirstPlay();
	await tick();
	await tick();
	assert.equal(startPosts, 1, 'if first play does not start REC then auto-REC is broken');
});

test('a second play in the same session does not start a second recording', async () => {
	autoRec.resetSetAutoRecSession();
	autoRec.setSetAutoRecEnabledForTest(true);
	installFetch({ active: false });
	autoRec.maybeStartAutoRecOnFirstPlay();
	autoRec.maybeStartAutoRecOnFirstPlay();
	await tick();
	await tick();
	assert.equal(startPosts, 1, 'if a second play starts REC again then auto-REC is broken');
});
