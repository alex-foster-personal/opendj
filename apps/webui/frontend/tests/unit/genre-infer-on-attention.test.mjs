/**
 * GENRE-12623: selecting or loading without a chosen genre calls /genres/enqueue.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://genre-attention.example.test';
const attention = await loadTypeScriptModule('src/lib/rb/genre-infer-on-attention.ts');
attention.resetGenreInferOnAttentionForTests();
await loadTypeScriptModule('src/lib/rb/api-genres.ts', { viteApiBase: API_BASE });

let originalFetch;
let posts = [];

before(() => {
	originalFetch = globalThis.fetch;
	globalThis.fetch = async (url, init) => {
		if (String(url).endsWith('/api/v1/genres/enqueue')) {
			posts.push(JSON.parse(String(init?.body ?? '{}')));
			return new Response(JSON.stringify({ lane: 'genre', queued: 1, requested: 1, eligible: 1 }), {
				status: 201
			});
		}
		return originalFetch(url, init);
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('requestGenreInferOnAttention posts stable_ids once', async () => {
	posts.length = 0;
	attention.resetGenreInferOnAttentionForTests();
	attention.requestGenreInferOnAttention(['sid-a', 'sid-b']);
	await new Promise((resolve) => setTimeout(resolve, 5));
	assert.equal(posts.length, 1);
	assert.deepEqual(posts[0].stable_ids, ['sid-a', 'sid-b']);
});

test('requestGenreInferOnAttention coalesces a second call while in flight', async () => {
	posts.length = 0;
	attention.resetGenreInferOnAttentionForTests();
	let release;
	const slow = new Promise((resolve) => {
		release = resolve;
	});
	const oldFetch = globalThis.fetch;
	globalThis.fetch = async (url, init) => {
		if (String(url).endsWith('/api/v1/genres/enqueue')) {
			posts.push(JSON.parse(String(init?.body ?? '{}')));
			await slow;
			return new Response(JSON.stringify({ lane: 'genre', queued: 1, requested: 1, eligible: 1 }), {
				status: 201
			});
		}
		return oldFetch(url, init);
	};
	attention.requestGenreInferOnAttention(['sid-a']);
	attention.requestGenreInferOnAttention(['sid-b']);
	release();
	await new Promise((resolve) => setTimeout(resolve, 5));
	assert.equal(posts.length, 1);
	globalThis.fetch = oldFetch;
});
