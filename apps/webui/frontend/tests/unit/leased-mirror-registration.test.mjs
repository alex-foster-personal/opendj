/**
 * AGENT-19: the publisher tells the order poll the moment the page is
 * registered, and an out-of-order PUT does not unregister a page the engine
 * still has on record.
 *
 * [if] whenRegistered() does not settle on the first accepted PUT [then ⛔] a
 *   hidden tab's order poll waits on a throttled timer (up to a minute).
 * [if] a 409 stale_snapshot clears registration [then ⛔] the poll parks on that
 *   timer although the engine kept this page's newer snapshot.
 * [if] a 409 lease_held keeps registration [then ⛔] a demoted tab claims orders
 *   (mutation control for the rule above: only stale_snapshot is exempt).
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const { createTabLeadership } = await loadTypeScriptModule('src/lib/rb/tab-leadership.ts');
const { createLeasedMirrorPublisher, MIRROR_PATH, LEASE_PATH } = await loadTypeScriptModule(
	'src/lib/rb/leased-mirror-publisher.ts'
);

let realFetch;
let putReplies;

const json = (status, body) =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const flush = async () => {
	for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

beforeEach(() => {
	realFetch = globalThis.fetch;
	putReplies = [];
	globalThis.fetch = async (url, init = {}) => {
		if (url === LEASE_PATH) return json(200, { held: false, holder: null });
		if (url === MIRROR_PATH && init.method === 'PUT') return putReplies.shift() ?? json(202, { accepted: true });
		throw new Error(`unexpected request ${init.method ?? 'GET'} ${url}`);
	};
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

function publisher() {
	const leadership = createTabLeadership({ locks: null });
	return createLeasedMirrorPublisher({
		leadership,
		clientId: 'tab-a',
		build: () => ({ client_open: true, client_id: 'tab-a' }),
		isPlaying: () => false,
		isVisible: () => false
	});
}

/** Lease preflight, then the first accepted PUT. */
async function register(mirror) {
	mirror.publish();
	await flush();
	mirror.publish();
	await flush();
}

test('whenRegistered settles on the first accepted PUT, with no timer', async () => {
	const mirror = publisher();
	let settled = false;
	void mirror.whenRegistered().then(() => {
		settled = true;
	});
	await flush();
	assert.equal(settled, false, 'nothing accepted yet');
	await register(mirror);
	assert.equal(mirror.isRegistered(), true);
	assert.equal(settled, true, 'the waiting poll is woken by the accepted PUT itself');
});

test('a stale_snapshot 409 leaves a registered page registered', async () => {
	const mirror = publisher();
	await register(mirror);
	putReplies.push(json(409, { accepted: false, reason: 'stale_snapshot' }));
	mirror.publish();
	await flush();
	assert.equal(mirror.isRegistered(), true, 'the engine still has this page on record');
});

test('mutation control: a lease_held 409 does unregister the page', async () => {
	const mirror = publisher();
	await register(mirror);
	putReplies.push(json(409, { accepted: false, reason: 'lease_held', held: true, holder: 'tab-b' }));
	mirror.publish();
	await flush();
	assert.equal(mirror.isRegistered(), false, 'a demoted tab must stop claiming orders');
});
