// SESSION-PLAYED-01: the session played set (green library rows) survives
// crash and relaunch, asks before reviving an expired session, and a New
// session clears it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { SessionPlayedTracker, formatSessionWhen } from '../../src/lib/rb/session-played.ts';

const HOUR = 3_600_000;

function fakeServer(initial) {
	const doc = { ...initial };
	const calls = [];
	const status = () => ({ ...doc, played_ids: [...doc.played_ids], count: doc.played_ids.length });
	return {
		calls,
		api: {
			async get() {
				calls.push('get');
				return status();
			},
			async add(id) {
				calls.push(`add:${id}`);
				if (!doc.played_ids.includes(id)) doc.played_ids = [...doc.played_ids, id];
				doc.expired = false;
				return status();
			},
			async resume() {
				calls.push('resume');
				doc.expired = false;
				return status();
			},
			async startNew() {
				calls.push('new');
				doc.session_id = 'n';
				doc.played_ids = [];
				doc.expired = false;
				return status();
			}
		}
	};
}

const live = (ids) => ({ session_id: 's', started_at_ms: 0, last_activity_ms: Date.now() - 3 * HOUR, expired: false, played_ids: ids });

test('a relaunch (fresh tracker over the stored doc) shows every played id', async () => {
	const server = fakeServer(live(['a', 'b']));
	const tracker = new SessionPlayedTracker(server.api);
	await tracker.boot();
	assert.equal(tracker.phase, 'live');
	assert.deepEqual([...tracker.ids], ['a', 'b'], 'if a relaunch 3 h later loses the green rows then broken');
});

test('loads made before boot finishes are posted once boot lands, never lost', async () => {
	const server = fakeServer(live(['a']));
	const tracker = new SessionPlayedTracker(server.api);
	tracker.record('c');
	await tracker.boot();
	await tracker.settled();
	assert.deepEqual([...tracker.ids].sort(), ['a', 'c']);
	assert.ok(server.calls.includes('add:c'));
});

test('a known id is not posted again', async () => {
	const server = fakeServer(live(['a']));
	const tracker = new SessionPlayedTracker(server.api);
	await tracker.boot();
	tracker.record('a');
	await tracker.settled();
	assert.ok(!server.calls.includes('add:a'));
});

test('an expired session hides the green rows and holds loads until the DJ answers', async () => {
	const server = fakeServer({ ...live(['old']), expired: true });
	const tracker = new SessionPlayedTracker(server.api);
	await tracker.boot();
	assert.equal(tracker.phase, 'expired');
	assert.deepEqual([...tracker.visibleIds], [], 'if an expired session paints green without asking then broken');
	assert.deepEqual([...tracker.ids], ['old'], 'if expiry drops the stored ids then broken');
	tracker.record('fresh');
	await tracker.settled();
	assert.ok(!server.calls.includes('add:fresh'), 'if a load silently revives an expired session then broken');
	await tracker.resume();
	assert.equal(tracker.phase, 'live');
	assert.deepEqual([...tracker.visibleIds].sort(), ['fresh', 'old']);
});

test('New session clears the played set, then keeps the held loads', async () => {
	const server = fakeServer({ ...live(['old']), expired: true });
	const tracker = new SessionPlayedTracker(server.api);
	await tracker.boot();
	tracker.record('fresh');
	await tracker.startNew();
	assert.deepEqual([...tracker.visibleIds], ['fresh'], 'if New session keeps old played rows then broken');
});

test('a failed boot reads as error, not as an empty session', async () => {
	const api = fakeServer(live([])).api;
	api.get = async () => {
		throw new Error('500');
	};
	const tracker = new SessionPlayedTracker(api);
	await tracker.boot();
	assert.equal(tracker.phase, 'error');
	assert.match(tracker.error ?? '', /500/);
});

test('formatSessionWhen carries the weekday', () => {
	const ms = new Date(2026, 10, 4, 20, 14).getTime();
	assert.equal(formatSessionWhen(ms), 'Wed 20:14');
});
