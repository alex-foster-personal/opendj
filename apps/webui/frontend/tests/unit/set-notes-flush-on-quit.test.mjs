/**
 * SETNOTE-9951: the last set-note edit is saved when the desktop app quits right after typing.
 *
 * [if] a note typed less than the debounce before quit is not PUT and awaited before exitShell,
 * a failed save still quits, or a dirty draft kept on pagehide is not restored over an older
 * server copy [then] fail, [else stop].
 */
import assert from 'node:assert/strict';
import { beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'http://set-notes.test';
const ENTRY = 'tests/unit/fixtures/set-notes-quit-entry.ts';

function fakeStorage() {
	const items = new Map();
	return {
		getItem: (key) => (items.has(key) ? items.get(key) : null),
		setItem: (key, value) => items.set(key, String(value)),
		removeItem: (key) => items.delete(key),
		items
	};
}

function json(status, body) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function note(body, rev, updatedAt) {
	return {
		scope: 'scratch',
		body_md: body,
		rev,
		updated_at: updatedAt,
		x: null,
		y: null,
		w: null,
		h: null,
		visible: true
	};
}

/** A fake transport: GET answers `getNote`, PUT answers after `putDelayMs` with `putStatus`. */
function installFetch({ getNote = null, putStatus = 200, putDelayMs = 30 } = {}) {
	const events = [];
	const puts = [];
	globalThis.fetch = async (input, init) => {
		const request = input instanceof Request ? input : new Request(input, init);
		const url = new URL(request.url);
		if (!url.pathname.startsWith('/api/v1/set-notes/')) return new Response(null, { status: 204 });
		if (request.method === 'GET') {
			return getNote === null ? json(404, { detail: 'not found' }) : json(200, getNote);
		}
		const body = await request.json();
		puts.push(body);
		events.push('put-sent');
		await new Promise((resolve) => setTimeout(resolve, putDelayMs));
		events.push('put-settled');
		if (putStatus !== 200) return json(putStatus, { detail: 'boom' });
		return json(200, note(body.body_md, body.base_rev + 1, new Date().toISOString()));
	};
	return { events, puts };
}

let storage;

beforeEach(() => {
	storage = fakeStorage();
	globalThis.window = { localStorage: storage, addEventListener() {}, removeEventListener() {} };
});

async function freshStore() {
	return loadTypeScriptModule(ENTRY, { viteApiBase: API_BASE });
}

test('type, quit within 100 ms: the PUT is sent and awaited before exitShell', async () => {
	const { events, puts } = installFetch();
	const mod = await freshStore();
	assert.equal(await mod.loadSetNote('scratch'), true);
	mod.editSetNote('last words before quit');
	await new Promise((resolve) => setTimeout(resolve, 50));
	assert.deepEqual(events, [], 'the 600 ms debounce has not fired yet');
	await mod.confirmQuit({
		flushSnapshot: () => {},
		exitShell: async () => void events.push('exit')
	});
	assert.deepEqual(events, ['put-sent', 'put-settled', 'exit']);
	assert.equal(puts[0].body_md, 'last words before quit');
	assert.equal(mod.setNote.dirty, false);
});

test('a failed note save takes the quit gate error path instead of quitting', async () => {
	const { events } = installFetch({ putStatus: 500 });
	const mod = await freshStore();
	await mod.loadSetNote('scratch');
	mod.editSetNote('unsaved');
	await mod.confirmQuit({
		flushSnapshot: () => {},
		exitShell: async () => void events.push('exit')
	});
	assert.equal(events.includes('exit'), false, 'the app stays open');
	assert.equal(mod.setNote.dirty, true, 'the draft is kept');
	await assert.rejects(mod.flushPendingNote(), /Could not save note/);
});

test('flushPendingNote is a no-op when nothing is pending', async () => {
	const { puts } = installFetch();
	const mod = await freshStore();
	await mod.loadSetNote('scratch');
	await mod.flushPendingNote();
	assert.equal(puts.length, 0);
});

test('a dirty draft kept on pagehide is restored over an older server copy', async () => {
	installFetch({ getNote: note('server text', 3, '2026-11-01T10:00:00Z') });
	const first = await freshStore();
	await first.loadSetNote('scratch');
	first.editSetNote('typed then reloaded');
	first.persistSetNoteDraft();
	assert.ok(storage.items.has('opendj.set-notes.draft'), 'the draft is written on pagehide');
	// The page is gone: its pending debounced save must not run (and clear the kept draft) while the reload loads.
	first.setNote.dirty = false;

	const { puts } = installFetch({ getNote: note('server text', 3, '2026-11-01T10:00:00Z') });
	const reloaded = await freshStore();
	await reloaded.loadSetNote('scratch');
	assert.equal(reloaded.setNote.body, 'typed then reloaded');
	assert.equal(reloaded.setNote.dirty, true);
	await reloaded.flushPendingNote();
	assert.equal(puts.at(-1).body_md, 'typed then reloaded');
	assert.equal(storage.items.has('opendj.set-notes.draft'), false, 'a saved draft is cleared');
});

test('a kept draft older than the server copy is dropped', async () => {
	storage.setItem(
		'opendj.set-notes.draft',
		JSON.stringify({
			scope: 'scratch',
			body: 'stale draft',
			savedAtMs: Date.parse('2026-11-01T09:00:00Z')
		})
	);
	installFetch({ getNote: note('newer server text', 4, '2026-11-01T10:00:00Z') });
	const mod = await freshStore();
	await mod.loadSetNote('scratch');
	assert.equal(mod.setNote.body, 'newer server text');
	assert.equal(mod.setNote.dirty, false);
	assert.equal(storage.items.has('opendj.set-notes.draft'), false);
});
