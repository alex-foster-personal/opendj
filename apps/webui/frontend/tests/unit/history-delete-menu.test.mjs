// PLAYS-10523: history delete menus and confirm flow.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://history-delete.example.test';

let registry;
let historyDelete;

before(async () => {
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	historyDelete = await loadTypeScriptModule('src/lib/history/history-delete.ts', {
		viteApiBase: API_BASE
	});
});

test('history-row menu offers delete-play', () => {
	const items = registry.menuFor({ kind: 'history-row' }, {});
	assert.equal(items.length, 1);
	assert.equal(items[0].id, 'delete-play');
	assert.equal(items[0].command, 'history.delete_play');
	assert.equal(items[0].enabled, true);
});

test('history-day menu respects empty flag', () => {
	const enabled = registry.menuFor({ kind: 'history-day', date: '2026-11-07' }, { empty: false });
	assert.equal(enabled[0].enabled, true);
	const disabled = registry.menuFor({ kind: 'history-day', date: '2026-11-07' }, { empty: true });
	assert.equal(disabled[0].enabled, false);
	assert.ok((disabled[0].disabledReason ?? '').trim() !== '');
});

test('confirmAndDeletePlay respects cancel', async () => {
	let removed = 0;
	let refreshed = 0;
	const ok = await historyDelete.confirmAndDeletePlay(
		{
			ask: async () => ({ ok: false }),
			remove: async () => {
				removed += 1;
			},
			refresh: () => {
				refreshed += 1;
			},
			toast: () => {}
		},
		{ title: 't', message: 'm', done: 'd' }
	);
	assert.equal(ok, false);
	assert.equal(removed, 0);
	assert.equal(refreshed, 0);
});

test('confirmAndDeletePlay runs remove then refresh', async () => {
	const calls = [];
	const ok = await historyDelete.confirmAndDeletePlay(
		{
			ask: async () => ({ ok: true }),
			remove: async () => {
				calls.push('remove');
			},
			refresh: () => {
				calls.push('refresh');
			},
			toast: (msg) => calls.push(`toast:${msg}`)
		},
		{ title: 't', message: 'm', done: 'done-msg' }
	);
	assert.equal(ok, true);
	assert.deepEqual(calls, ['remove', 'refresh', 'toast:done-msg']);
});

test('confirmAndDeletePlay error skips refresh', async () => {
	let refreshed = 0;
	const ok = await historyDelete.confirmAndDeletePlay(
		{
			ask: async () => ({ ok: true }),
			remove: async () => {
				throw new Error('nope');
			},
			refresh: () => {
				refreshed += 1;
			},
			toast: () => {}
		},
		{ title: 't', message: 'm', done: 'd' }
	);
	assert.equal(ok, false);
	assert.equal(refreshed, 0);
});

test('deleteHistoryPlay uses DELETE on the play route', async () => {
	let seen;
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		seen = { url: request.url, method: request.method };
		return new Response(
			JSON.stringify({
				stable_id: 'a'.repeat(40),
				play_count: 1,
				rekordbox_play_count: 0,
				opendj_play_count: 1,
				opendj_last_played_at: null,
				last_played_at: null
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	};
	const sid = 'a'.repeat(40);
	await historyDelete.deleteHistoryPlay(sid, 'play-id-12', globalThis.fetch);
	assert.equal(seen.method, 'DELETE');
	assert.equal(seen.url, `${API_BASE}/api/v1/tracks/${sid}/plays/play-id-12`);
});

test('deleteHistoryDay uses DELETE on the history route', async () => {
	let seen;
	globalThis.fetch = async (url, init) => {
		seen = { url, init };
		return new Response(
			JSON.stringify({ date: '2026-11-07', start: 's', end: 'e', removed: 2, stable_ids: [] }),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	};
	await historyDelete.deleteHistoryDay('2026-11-07', globalThis.fetch);
	assert.equal(seen.init.method, 'DELETE');
	assert.equal(seen.url, `${API_BASE}/api/v1/history/2026-11-07`);
});

test('deleteHistoryPlay 404 rejects', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { code: 'PLAY_NOT_FOUND', message: 'missing' } }), {
			status: 404,
			headers: { 'content-type': 'application/json' }
		});
	await assert.rejects(
		historyDelete.deleteHistoryPlay('a'.repeat(40), 'missing-99', globalThis.fetch),
		/missing|404/
	);
});
