import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = `export { TreeRecentlyDeleted } from '$lib/components/rb/browser/tree-recently-deleted.svelte.ts';`;

function flush() {
	return new Promise((resolve) => setImmediate(resolve));
}

function makeSubscriptions() {
	const kinds = new Map();
	let resync;
	return {
		subscribeKind(kind, listener) {
			kinds.set(kind, listener);
			return () => kinds.delete(kind);
		},
		subscribeResync(listener) {
			resync = listener;
			return () => (resync = undefined);
		},
		fireKind(kind) {
			kinds.get(kind)?.([], {});
		},
		fireResync() {
			resync?.('gap');
		}
	};
}

async function loadClass() {
	return (await loadRuneModule(ENTRY)).TreeRecentlyDeleted;
}

test('constructor lists with open default false', async () => {
	const TreeRecentlyDeleted = await loadClass();
	const subscriptions = makeSubscriptions();
	const rows = [
		{
			playlist_id: 'pl-1',
			name: 'Gone',
			vendor: 'webui',
			vendor_pl_id: 'gone',
			deleted_at: '2026-09-13T00:00:00Z',
			updated_at: '2026-09-13T00:00:01Z',
			track_count: 1
		}
	];
	const store = new TreeRecentlyDeleted(
		async () => rows,
		async () => ({}),
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	await flush();
	assert.equal(store.open, false);
	assert.deepEqual(store.rows, rows);
	assert.equal(store.error, null);
	store.destroy();
});

test('playlist events reload the list', async () => {
	const TreeRecentlyDeleted = await loadClass();
	const subscriptions = makeSubscriptions();
	let calls = 0;
	const store = new TreeRecentlyDeleted(
		async () => {
			calls += 1;
			return [];
		},
		async () => ({}),
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	await flush();
	assert.equal(calls, 1);
	subscriptions.fireKind('playlists');
	await flush();
	assert.equal(calls, 2);
	store.destroy();
});

test('restore calls undelete and drops the row locally', async () => {
	const TreeRecentlyDeleted = await loadClass();
	const subscriptions = makeSubscriptions();
	const restored = [];
	const store = new TreeRecentlyDeleted(
		async () => [
			{
				playlist_id: 'pl-1',
				name: 'Gone',
				vendor: 'webui',
				vendor_pl_id: 'gone',
				deleted_at: '2026-09-13T00:00:00Z',
				updated_at: '2026-09-13T00:00:01Z',
				track_count: 1
			}
		],
		async (id) => {
			restored.push(id);
			return {};
		},
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	await flush();
	await store.restore('pl-1');
	assert.deepEqual(restored, ['pl-1']);
	assert.deepEqual(store.rows, []);
	store.destroy();
});

test('backend errors surface on error', async () => {
	const TreeRecentlyDeleted = await loadClass();
	const subscriptions = makeSubscriptions();
	const store = new TreeRecentlyDeleted(
		async () => {
			throw Object.assign(new Error('boom'), { code: 'STATE_DB_MISSING' });
		},
		async () => ({}),
		subscriptions.subscribeKind,
		subscriptions.subscribeResync
	);
	await flush();
	assert.equal(store.error, 'STATE_DB_MISSING');
	store.destroy();
});
