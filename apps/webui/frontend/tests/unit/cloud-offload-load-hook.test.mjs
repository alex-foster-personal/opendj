/**
 * requirement: OFFLOAD-8598h
 *
 * The shared deck-load hook and progress polling (#8601 review round 1).
 * offload-ops runs for real; only `fetch` (the engine) is a test double.
 *
 * - [if] any surface loads an offloaded track [then] the hook restores it for 24 h first, [else stop].
 * - [if] a local track loads [then] the hook makes one state read and nothing else, [else stop].
 * - [if] the offload state cannot be read [then] the load still goes ahead, [else stop].
 * - [if] progress reads keep failing [then] the line ends failed with the reason, not stuck running, [else stop].
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

const SID = 'f'.repeat(40);
const BASE = 'https://offload-hook.example.test';

function operation(over = {}) {
	return {
		op_id: 'op1', kind: 'restore', mode: 'temporary', playlist_id: null, state: 'done', stable_ids: [SID],
		tracks_total: 1, tracks_done: 1, bytes_total: 10, bytes_done: 10, bytes_freed: 0, current: null,
		results: { [SID]: { state: 'temporary', note: null, error: null } }, error: null, dry_run: false, ...over
	};
}

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

async function withEngine(handler, body) {
	const originalFetch = globalThis.fetch;
	const calls = [];
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		const url = new URL(request.url);
		calls.push(`${request.method} ${url.pathname}`);
		return handler(request, url);
	};
	try {
		return await body(calls);
	} finally {
		globalThis.fetch = originalFetch;
	}
}

test('a load of an offloaded track restores it for 24h on that deck first', async () => {
	const ops = await loadTypeScriptModule('src/lib/offload/offload-ops.svelte.ts', { viteApiBase: BASE });
	await withEngine(
		async (request, url) => {
			if (url.pathname === '/api/v1/offload/tracks') return json({ [SID]: { state: 'offloaded', expires_at: null } });
			if (url.pathname === '/api/v1/offload/restore') {
				assert.deepEqual(JSON.parse(await request.text()), { stable_ids: [SID], mode: 'temporary' });
				return json(operation({ state: 'running' }), 202);
			}
			if (url.pathname === '/api/v1/offload/operations/op1') return json(operation());
			return json({ detail: 'unexpected' }, 500);
		},
		async (calls) => {
			await ops.ensureLocalBeforeLoad(2, SID);
			assert.deepEqual(calls, ['GET /api/v1/offload/tracks', 'POST /api/v1/offload/restore', 'GET /api/v1/offload/operations/op1']);
			assert.equal(ops.offloadOps.deckDownloads[2], undefined, 'the deck bar is gone once the track is back');
		}
	);
});

test('a load of a local track costs one state read and nothing else', async () => {
	const ops = await loadTypeScriptModule('src/lib/offload/offload-ops.svelte.ts', { viteApiBase: BASE });
	await withEngine(
		async () => json({ [SID]: null }),
		async (calls) => {
			await ops.ensureLocalBeforeLoad(1, SID);
			assert.deepEqual(calls, ['GET /api/v1/offload/tracks']);
		}
	);
});

test('a failed download fails the load with the reason', async () => {
	const ops = await loadTypeScriptModule('src/lib/offload/offload-ops.svelte.ts', { viteApiBase: BASE });
	await withEngine(
		async (_request, url) => {
			if (url.pathname === '/api/v1/offload/tracks') return json({ [SID]: { state: 'offloaded', expires_at: null } });
			if (url.pathname === '/api/v1/offload/restore') return json(operation({ state: 'running' }), 202);
			return json(operation({ state: 'failed', results: { [SID]: { state: null, note: null, error: 'OffloadVerifyError', message: 'bad bytes' } } }));
		},
		async () => {
			await assert.rejects(ops.ensureLocalBeforeLoad(1, SID), /TRACK_OFFLOADED/);
		}
	);
});

test('progress reads that keep failing end the line as failed, never stuck running', async () => {
	const ops = await loadTypeScriptModule('src/lib/offload/offload-ops.svelte.ts', { viteApiBase: BASE });
	await withEngine(
		async () => json({ detail: { code: 'ENGINE_GONE', message: 'gone' } }, 503),
		async () => {
			let finished = 0;
			const stop = ops.offloadOps.onFinished(() => (finished += 1));
			const final = await ops.offloadOps.follow(operation({ state: 'running', kind: 'offload' }));
			stop();
			assert.equal(final.state, 'failed');
			assert.match(final.error, /^progress unavailable: /);
			assert.equal(finished, 1, 'listeners still hear that the line ended');
		}
	);
});

test('an unreadable offload state never blocks a deck load', async () => {
	const ops = await loadTypeScriptModule('src/lib/offload/offload-ops.svelte.ts', { viteApiBase: BASE });
	const originalWarn = console.warn;
	const warnings = [];
	console.warn = (...args) => warnings.push(args.join(' '));
	try {
		await withEngine(
			async () => json({ detail: 'down' }, 500),
			async (calls) => {
				await ops.ensureLocalBeforeLoad(3, SID);
				assert.deepEqual(calls, ['GET /api/v1/offload/tracks']);
			}
		);
	} finally {
		console.warn = originalWarn;
	}
	assert.match(warnings.join('\n'), /state of f+ unreadable, loading without a restore/);
});
