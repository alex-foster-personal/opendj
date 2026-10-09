/**
 * LIBM-181 (the maintainer, Tue 6 Oct 2026): "artwork still getting lost in library".
 * One failed cover load used to write artwork:false for the whole session.
 *
 * @pytest.mark.requirement LIBM-181
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const TABLE = readFileSync('src/lib/components/rb/browser/TrackTable.svelte', 'utf8');

let m;
before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/artwork-retry-entry.ts');
});
beforeEach(() => m.resetOptionalResourcesForTests());

/** Fake clock: records each requested delay; advance(ms) fires what is due. */
function fakeTimers() {
	let now = 0;
	let nextId = 1;
	const pending = new Map();
	return {
		delays: [],
		setTimeout(fn, ms) {
			const id = nextId++;
			this.delays.push(ms);
			pending.set(id, { at: now + ms, fn });
			return id;
		},
		clearTimeout(id) {
			pending.delete(id);
		},
		advance(ms) {
			now += ms;
			for (const [id, t] of [...pending]) {
				if (t.at <= now) {
					pending.delete(id);
					t.fn();
				}
			}
		}
	};
}

test('if an image error hides the cover past 2 s, or writes artwork:false, one network blip loses it for the session', () => {
	const timers = fakeTimers();
	const retry = new m.ArtworkRetry(timers);
	m.rememberOptionalResources('a', { artwork: true });
	assert.equal(m.artworkFetchAllowed('a', retry), true);
	retry.noteError('a');
	assert.equal(m.artworkFetchAllowed('a', retry), false, 'hidden while the 2 s retry is pending');
	timers.advance(1999);
	assert.equal(m.artworkFetchAllowed('a', retry), false);
	timers.advance(1);
	assert.equal(m.artworkFetchAllowed('a', retry), true, 'due again 2 s after the error');
	assert.equal(m.artworkRetrySrc('/api/v1/tracks/a/artwork?size=s', 'a', retry), '/api/v1/tracks/a/artwork?size=s&r=1');
	// TrackTable no longer records an error as "no artwork".
	assert.doesNotMatch(TABLE, /rememberOptionalResources\([^)]*artwork:\s*false/);
	assert.match(TABLE, /onerror=\{\(\) => _onArtworkError\(row\.stable_id\)\}/);
	assert.match(TABLE, /coverRetry\.noteError\(stableId\)/);
});

test('if a row the server says has no artwork ever fetches, the retry overrode the verdict', () => {
	const retry = new m.ArtworkRetry(fakeTimers());
	m.rememberOptionalResources('none', { artwork: false });
	m.rememberOptionalResources('unreadable', { artwork: null });
	assert.equal(m.artworkFetchAllowed('none', retry), false);
	assert.equal(m.artworkFetchAllowed('unreadable', retry), false);
	// Control: a fresh id with no verdict and no errors fetches.
	assert.equal(m.artworkFetchAllowed('fresh', retry), true);
});

test('if retries never stop, a missing file is fetched forever; after the 3rd failed retry it gives up', () => {
	const timers = fakeTimers();
	const retry = new m.ArtworkRetry(timers);
	for (let i = 0; i < 3; i++) {
		retry.noteError('b');
		assert.equal(retry.givenUp('b'), false, `still retrying after ${i + 1} failure(s)`);
		timers.advance(30000);
		assert.equal(m.artworkFetchAllowed('b', retry), true);
	}
	assert.deepEqual(timers.delays, [2000, 8000, 30000]);
	retry.noteError('b');
	assert.equal(retry.givenUp('b'), true);
	timers.advance(10 * 60 * 1000);
	assert.equal(m.artworkFetchAllowed('b', retry), false, 'given up: no 4th retry');
	assert.equal(timers.delays.length, 3);
	// A load resets the count (control for the overshoot of "give up forever").
	const r2 = new m.ArtworkRetry(fakeTimers());
	r2.noteError('c');
	r2.noteLoad('c');
	assert.equal(r2.failures('c'), 0);
	assert.equal(m.artworkFetchAllowed('c', r2), true);
});

test('if a backend reconnect does not clear given-up covers, they stay blank after the backend is back', () => {
	const shared = m.artworkRetry();
	for (let i = 0; i < 4; i++) shared.noteError('d');
	shared.noteError('e'); // still waiting on its first retry: not given up
	assert.equal(shared.givenUp('d'), true);
	let socket;
	m.connectEventsBus('ws://events.example.test/api/v1/events', {
		socketFactory: (url) => {
			socket = { url, onopen: null, onmessage: null, onclose: null, onerror: null, close() {} };
			return socket;
		},
		scheduler: { setTimeout: () => 0, clearTimeout: () => {} }
	});
	socket.onopen?.({});
	socket.onmessage?.({
		data: JSON.stringify({
			topic: 'hello',
			seq: 0,
			ts: '2026-10-06T18:00:00.000Z',
			payload: { contract_rev: 'rev-1', engine_version: '1.0.0', seq_start: 0, topics: [] }
		})
	});
	assert.equal(shared.givenUp('d'), false);
	assert.equal(m.artworkFetchAllowed('d', shared), true);
	// Control: an id mid-retry keeps its pending retry rather than being reset.
	assert.equal(shared.failures('e'), 1);
});
