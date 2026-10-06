/**
 * AGENT-18: exactly one /performance tab per engine writes the UI mirror and
 * claims agent orders.
 *
 * Mon 5 Oct 2026 on the live preview: the maintainer's Chrome tab plus two agent browser
 * panes each polled `GET /api/v1/commands/next` every 50 ms and PUT the mirror
 * every second, so an order ran in whichever tab claimed it and the engine's
 * view of what was playing flipped between tabs.
 *
 * The Web Locks manager and the engine are fakes that implement the documented
 * contracts (ifAvailable, steal rejects the old holder with AbortError, a
 * signal aborts a queued request; the lease rules of routes/state.py). The
 * controller, the publisher and the order poll are the real modules.
 *
 * Regression lines:
 *   - if a second tab in one browser is not a follower then broken
 *   - if closing the leader does not promote a follower then broken
 *   - if Take control does not move leadership (and hand it back later) then broken
 *   - if a follower PUTs the mirror or polls commands/next then broken
 *   - if a 409 lease_held does not demote the tab then broken
 *   - if a demoted tab keeps PUTting (409 console spam) then broken
 *   - if a lease release does not promote the other browser's tab then broken
 *   - if a reloaded page PUTs into the previous page's held lease (a 409) then broken
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const leadershipModule = await loadTypeScriptModule('src/lib/rb/tab-leadership.ts');
const publisherModule = await loadTypeScriptModule('src/lib/rb/leased-mirror-publisher.ts');
const orders = await loadTypeScriptModule('src/lib/rb/agent-orders.ts');
const { createTabLeadership, confirmedLeadership, whileLeader } = leadershipModule;
const { createLeasedMirrorPublisher, decideFollowerClaim, MIRROR_PATH, LEASE_PATH, LEASE_RECHECK_MS } = publisherModule;
const NEXT = orders.NEXT_ORDER_URL;

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function flush() {
	for (let i = 0; i < 10; i += 1) await tick();
}

function abortError() {
	const error = new Error('lock request aborted');
	error.name = 'AbortError';
	return error;
}

/** One browser's LockManager, single lock name, per the Web Locks spec. */
function fakeLocks() {
	let holder = null;
	const queue = [];
	const grant = (entry) => {
		holder = entry;
		Promise.resolve()
			.then(() => entry.callback({ name: entry.name }))
			.then(
				(value) => {
					if (holder !== entry) return;
					holder = null;
					entry.resolve(value);
					next();
				},
				(error) => {
					if (holder !== entry) return;
					holder = null;
					entry.reject(error);
					next();
				}
			);
	};
	const next = () => {
		const entry = queue.shift();
		if (entry !== undefined) grant(entry);
	};
	return {
		held: () => holder !== null,
		request(name, options, callback) {
			return new Promise((resolve, reject) => {
				const entry = { name, callback, resolve, reject };
				if (options.steal) {
					if (holder !== null) {
						const stolen = holder;
						holder = null;
						stolen.reject(abortError());
					}
					grant(entry);
					return;
				}
				if (holder === null && queue.length === 0) {
					grant(entry);
					return;
				}
				if (options.ifAvailable) {
					Promise.resolve(callback(null)).then(resolve, reject);
					return;
				}
				queue.push(entry);
				options.signal?.addEventListener('abort', () => {
					const index = queue.indexOf(entry);
					if (index >= 0) queue.splice(index, 1);
					reject(abortError());
				});
			});
		}
	};
}

function tab(locks) {
	const snapshots = [];
	const leadership = createTabLeadership({ locks, onChange: (snapshot) => snapshots.push(snapshot) });
	return { leadership, snapshots, role: () => leadership.snapshot().role };
}

// ------------------------------------------------------------ leadership ---

test('the first tab leads and a second tab in the same browser follows', async () => {
	const locks = fakeLocks();
	const a = tab(locks);
	await flush();
	const b = tab(locks);
	await flush();
	assert.equal(a.role(), 'leader');
	assert.equal(b.role(), 'follower');
	assert.equal(b.leadership.snapshot().reason, 'another-tab');
	assert.equal(b.leadership.isLeader(), false);
	a.leadership.dispose();
	b.leadership.dispose();
});

test('mutation control: without a shared lock both tabs lead', async () => {
	// This is the defect: each tab on its own lock (or none) believes it leads.
	const a = tab(fakeLocks());
	const b = tab(fakeLocks());
	await flush();
	assert.equal(a.role(), 'leader');
	assert.equal(b.role(), 'leader');
});

test('closing the leader promotes the follower', async () => {
	const locks = fakeLocks();
	const a = tab(locks);
	await flush();
	const b = tab(locks);
	await flush();
	a.leadership.dispose();
	await flush();
	assert.equal(b.role(), 'leader');
	assert.deepEqual(b.snapshots.map((s) => s.role), ['follower', 'leader']);
	b.leadership.dispose();
});

test('Take control moves leadership, and it comes back when the taker closes', async () => {
	const locks = fakeLocks();
	const a = tab(locks);
	await flush();
	const b = tab(locks);
	await flush();
	b.leadership.takeControl();
	await flush();
	assert.equal(b.role(), 'leader');
	assert.equal(a.role(), 'follower', 'the stolen tab must stand down');
	assert.equal(b.leadership.consumeTakeover(), true, 'the next PUT asks the engine for the lease');
	assert.equal(b.leadership.consumeTakeover(), false, 'and only the next one');
	b.leadership.dispose();
	await flush();
	assert.equal(a.role(), 'leader', 'the stolen tab re-queued and got it back');
	a.leadership.dispose();
	await flush();
	assert.equal(locks.held(), false, 'dispose releases the lock');
});

test('a lease conflict demotes a lock holder and a free lease restores it', () => {
	const a = tab(null);
	assert.equal(a.role(), 'leader', 'no Web Locks: the lease alone arbitrates');
	a.leadership.noteLeaseConflict('chrome-tab');
	assert.deepEqual(a.leadership.snapshot(), {
		role: 'follower',
		reason: 'another-browser',
		leaseHolder: 'chrome-tab'
	});
	assert.equal(a.leadership.holdsLocalLock(), true);
	a.leadership.noteLeaseFree();
	assert.equal(a.role(), 'leader');
});

// --------------------------------------------- publisher + fake engine ---

const TTL_MS = 10_000;
let realFetch;
let engine;
let clockMs;

/** The lease rules of apps/webui/server/routes/state.py, in memory. */
function fakeEngine() {
	const state = { lease: null, mirror: null, requests: [], refusals: [], rules: { audible: true } };
	const live = () => (state.lease !== null && state.lease.expires > clockMs ? state.lease : null);
	const json = (status, body) =>
		new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
	state.fetch = async (url, init = {}) => {
		const method = init.method ?? 'GET';
		const headers = init.headers ?? {};
		state.requests.push({ method, url: String(url), lease: headers['x-opendj-lease'] ?? null, takeover: headers['x-opendj-lease-takeover'] ?? null });
		if (url === MIRROR_PATH && method === 'PUT') {
			const id = headers['x-opendj-lease'];
			const current = live();
			const claimant = JSON.parse(init.body);
			const holderDoc = current !== null && state.mirror?.client_id === current.holder ? state.mirror : null;
			const handover =
				holderDoc !== null &&
				((state.rules.audible && claimant.playing === true && holderDoc.playing !== true && current.operator !== true) ||
					(holderDoc.visible === false && holderDoc.playing !== true && claimant.visible !== false));
			if (current !== null && current.holder !== id && headers['x-opendj-lease-takeover'] !== '1' && !handover) {
				state.refusals.push(id);
				return json(409, { accepted: false, reason: 'lease_held', held: true, holder: current.holder });
			}
			const operator = headers['x-opendj-lease-takeover'] === '1' || (current !== null && current.holder === id && current.operator === true);
			state.lease = { holder: id, expires: clockMs + TTL_MS, operator };
			state.mirror = JSON.parse(init.body);
			return json(202, { accepted: true });
		}
		if (url === MIRROR_PATH && method === 'DELETE') {
			const current = live();
			if (current === null || current.holder === headers['x-opendj-client-id']) {
				state.lease = null;
				state.mirror = null;
			}
			return new Response(null, { status: 204 });
		}
		if (url === LEASE_PATH) {
			const current = live();
			const doc = current !== null && state.mirror?.client_id === current.holder ? state.mirror : null;
			return json(200, {
				held: current !== null,
				holder: current?.holder ?? null,
				holder_playing: doc === null ? null : doc.playing === true,
				holder_yieldable: doc === null ? null : doc.visible === false && doc.playing !== true,
				holder_operator_claimed: current === null ? null : current.operator === true
			});
		}
		if (url === NEXT) return json(200, null);
		throw new Error(`unexpected request ${method} ${url}`);
	};
	return state;
}

beforeEach(() => {
	realFetch = globalThis.fetch;
	clockMs = 1_000_000;
	engine = fakeEngine();
	globalThis.fetch = engine.fetch;
});

afterEach(() => {
	globalThis.fetch = realFetch;
});

/** One /performance page, wired the way installUiMirror wires it: promotion publishes at once. */
function page(locks, clientId, initial = {}) {
	const tabState = { playing: false, visible: true, ...initial };
	const snapshots = [];
	let mirror = null;
	const leadership = createTabLeadership({
		locks,
		onChange: (snapshot) => {
			const was = snapshots.at(-1)?.role;
			snapshots.push(snapshot);
			if (snapshot.role === 'leader' && was !== 'leader') mirror.publish();
		}
	});
	mirror = createLeasedMirrorPublisher({
		leadership,
		clientId,
		build: () => ({ client_open: true, client_id: clientId, playing: tabState.playing, visible: tabState.visible }),
		isPlaying: () => tabState.playing,
		isVisible: () => tabState.visible,
		now: () => clockMs
	});
	return { leadership, snapshots, mirror, clientId, tabState, role: () => leadership.snapshot().role };
}

const puts = (id) => engine.requests.filter((r) => r.method === 'PUT' && r.lease === id).length;

test('same browser: only the leader PUTs; closing it promotes the follower', async () => {
	const locks = fakeLocks();
	const a = page(locks, 'tab-a');
	await flush();
	const b = page(locks, 'tab-b');
	await flush();
	for (let i = 0; i < 5; i += 1) {
		a.mirror.publish();
		b.mirror.publish();
		clockMs += 1000;
		await flush();
	}
	assert.equal(puts('tab-a'), 6, 'one PUT on promotion, then one per tick');
	assert.equal(puts('tab-b'), 0, 'a follower is silent toward the engine');
	assert.equal(b.role(), 'follower', 'an idle visible follower with no gesture never claims');
	assert.equal(b.mirror.isRegistered(), false);
	// Close A the way installUiMirror's teardown does.
	await engine.fetch(MIRROR_PATH, { method: 'DELETE', headers: { 'x-opendj-client-id': 'tab-a' } });
	a.leadership.dispose();
	await flush();
	assert.equal(b.role(), 'leader');
	assert.equal(puts('tab-b'), 1, 'promotion publishes at once');
	assert.equal(b.mirror.isRegistered(), true);
	assert.equal(engine.mirror.client_id, 'tab-b');
	b.leadership.dispose();
});

test('two browsers: the second reads the lease and never PUTs into it', async () => {
	const a = page(fakeLocks(), 'chrome');
	await flush();
	const b = page(fakeLocks(), 'agent-pane');
	await flush();
	assert.equal(b.leadership.snapshot().reason, 'another-browser');
	assert.equal(b.leadership.snapshot().leaseHolder, 'chrome');
	for (let i = 0; i < 6; i += 1) {
		clockMs += 1000;
		a.mirror.publish();
		b.mirror.publish();
		await flush();
	}
	assert.equal(puts('agent-pane'), 0, 'the lease is read first, so not even one refused PUT');
	assert.deepEqual(engine.refusals, [], 'no 409 at all');
	const leaseReads = engine.requests.filter((r) => r.url === LEASE_PATH).length;
	assert.ok(leaseReads >= 4 && leaseReads <= 5, `one pre-PUT read, then every ${LEASE_RECHECK_MS} ms, got ${leaseReads}`);
	assert.equal(engine.mirror.client_id, 'chrome', 'the leader mirror was never overwritten');
	// Chrome closes: its DELETE releases the lease, and the pane takes over.
	await engine.fetch(MIRROR_PATH, { method: 'DELETE', headers: { 'x-opendj-client-id': 'chrome' } });
	clockMs += LEASE_RECHECK_MS;
	b.mirror.publish();
	await flush();
	assert.equal(b.role(), 'leader');
	assert.equal(engine.mirror.client_id, 'agent-pane', 'promotion published at once');
});

/** Reload page `old` in its own browser: the old document dies (its lock goes
 * with it) and a new document with a fresh client id and a free lock loads. */
function reload(old, clientId, { released }) {
	if (released) engine.fetch(MIRROR_PATH, { method: 'DELETE', headers: { 'x-opendj-client-id': old.clientId } });
	old.leadership.dispose();
	return page(fakeLocks(), clientId);
}

test('a reloaded page never PUTs into the previous page\'s held lease (main E2E run 37383098288)', async () => {
	const before = page(fakeLocks(), 'load-1');
	await flush();
	await run(2, before);
	assert.equal(engine.lease.holder, 'load-1', 'precondition: the first load holds the lease');
	// pagehide's keepalive DELETE never reached the engine (a killed page, a
	// closed Playwright context), so the old lease lives out its 10 s TTL.
	const after = reload(before, 'load-2', { released: false });
	await flush();
	assert.equal(after.role(), 'follower', 'it waits as a viewer while the old lease lives');
	assert.equal(after.leadership.snapshot().reason, 'another-browser');
	await run(TTL_MS / 1000 - 3, after);
	assert.equal(puts('load-2'), 0, 'no PUT while the old lease is live');
	await run(4, after);
	assert.equal(after.role(), 'leader', 'it leads once the old lease lapses');
	assert.equal(engine.mirror.client_id, 'load-2');
	assert.deepEqual(engine.refusals, [], 'not one 409, so Chromium logs no console error');
});

test('a reload whose pagehide released the lease leads at once, still without a 409', async () => {
	const before = page(fakeLocks(), 'load-1');
	await flush();
	const after = reload(before, 'load-2', { released: true });
	await flush();
	assert.equal(after.role(), 'leader');
	assert.equal(engine.mirror.client_id, 'load-2', 'the first PUT followed the lease read at once');
	assert.equal(puts('load-2'), 1);
	assert.deepEqual(engine.refusals, []);
});

test('two browsers: a crashed holder hands over after the lease lapses', async () => {
	const a = page(fakeLocks(), 'chrome');
	await flush();
	const b = page(fakeLocks(), 'agent-pane');
	await flush();
	clockMs += TTL_MS - 1000;
	b.mirror.publish();
	await flush();
	assert.equal(b.role(), 'follower', 'control: still held inside the TTL');
	clockMs += 2000;
	b.mirror.publish();
	await flush();
	assert.equal(b.role(), 'leader');
});

test('Take control across browsers sends the takeover header once and demotes the old holder', async () => {
	const a = page(fakeLocks(), 'chrome');
	await flush();
	const b = page(fakeLocks(), 'agent-pane');
	await flush();
	b.leadership.takeControl();
	await flush();
	assert.equal(engine.mirror.client_id, 'agent-pane', 'becoming leader publishes at once');
	clockMs += 1000;
	b.mirror.publish();
	a.mirror.publish();
	await flush();
	const takeovers = engine.requests.filter((r) => r.takeover === '1');
	assert.equal(takeovers.length, 1);
	assert.equal(a.leadership.snapshot().reason, 'another-browser');
	assert.equal(engine.mirror.client_id, 'agent-pane');
});

test('a follower never polls the order bus; a registered leader does', async () => {
	const locks = fakeLocks();
	const a = page(locks, 'tab-a');
	await flush();
	const b = page(locks, 'tab-b');
	await flush();
	a.mirror.publish();
	b.mirror.publish();
	await flush();
	let running = true;
	const followerPoll = orders.pollAgentOrders(b.mirror, b.mirror.publish, () => running);
	await new Promise((resolve) => setTimeout(resolve, 220));
	running = false;
	await followerPoll;
	assert.equal(engine.requests.filter((r) => r.url === NEXT).length, 0);
	running = true;
	const leaderPoll = orders.pollAgentOrders(a.mirror, a.mirror.publish, () => running);
	await new Promise((resolve) => setTimeout(resolve, 220));
	running = false;
	await leaderPoll;
	assert.ok(engine.requests.filter((r) => r.url === NEXT).length > 0, 'control: the leader does poll');
	a.leadership.dispose();
	b.leadership.dispose();
});

// ------------------------------------------- the right tab leads (CORE) ---

/** Advance the clock one second at a time, ticking every page like setInterval. */
async function run(seconds, ...pages) {
	for (let i = 0; i < seconds; i += 1) {
		clockMs += 1000;
		for (const p of pages) p.mirror.publish();
		await flush();
	}
}

test('decideFollowerClaim: the claim rules, each with its control', () => {
	const base = { selfId: 'me', holdsLocalLock: false, visible: true, playing: false, gestureAgeMs: null };
	const idleHolder = { held: true, holder: 'x', holder_playing: false, holder_yieldable: false };
	const playingHolder = { held: true, holder: 'x', holder_playing: true, holder_yieldable: false };
	const hiddenIdle = { held: true, holder: 'x', holder_playing: false, holder_yieldable: true };
	const free = { held: false, holder: null };
	const claim = (lease, extra = {}) => decideFollowerClaim({ ...base, lease, ...extra });
	assert.deepEqual(claim(idleHolder), { claim: false }, 'idle, visible, no gesture: viewer');
	assert.equal(claim(idleHolder, { playing: true }).why, 'audible');
	assert.deepEqual(claim(playingHolder, { playing: true }), { claim: false }, 'both playing: holder keeps it');
	assert.equal(claim(hiddenIdle).why, 'holder-hidden-idle');
	assert.deepEqual(claim(hiddenIdle, { visible: false }), { claim: false }, 'a hidden claimant never displaces');
	assert.deepEqual(claim(idleHolder, { gestureAgeMs: 500 }), { claim: true, takeover: true, why: 'gesture' });
	assert.deepEqual(claim(idleHolder, { gestureAgeMs: 10_001 }), { claim: false }, 'an old gesture is not a claim');
	assert.deepEqual(claim(playingHolder, { gestureAgeMs: 500 }), { claim: false }, 'a click never stops the set');
	assert.deepEqual(claim(free), { claim: false }, 'a lockless sibling never races the lock holder at open');
	assert.equal(claim(free, { holdsLocalLock: true }).why, 'free');
	assert.deepEqual(claim({ held: true, holder: 'me' }), { claim: false }, 'our stale lease is not a reason to steal back');
	assert.deepEqual(
		claim({ ...idleHolder, holder_operator_claimed: true }, { playing: true }),
		{ claim: false },
		'bug #31: the old leader, still playing for a moment, never takes Take control back'
	);
});

test('two browsers: a playing follower takes over from an idle leader within one lease period', async () => {
	const idle = page(fakeLocks(), 'idle-chrome');
	await flush();
	const playing = page(fakeLocks(), 'agent-pane');
	await flush();
	assert.equal(engine.lease.holder, 'idle-chrome', 'precondition: the idle tab got there first');
	assert.equal(playing.role(), 'follower', 'precondition: refused while it was idle');
	playing.tabState.playing = true;
	const startedAt = clockMs;
	let tookMs = null;
	for (let i = 0; i < 10 && tookMs === null; i += 1) {
		await run(1, idle, playing);
		if (engine.lease.holder === 'agent-pane') tookMs = clockMs - startedAt;
	}
	assert.ok(tookMs !== null && tookMs <= TTL_MS, `took ${tookMs} ms`);
	await run(2, idle, playing);
	assert.equal(playing.role(), 'leader');
	assert.equal(idle.role(), 'follower');
	assert.equal(engine.mirror.client_id, 'agent-pane', 'the mirror now reports the playing tab');
});

test('mutation control: with the audible rule off in the engine, the idle tab keeps the lease', async () => {
	engine.rules.audible = false;
	const idle = page(fakeLocks(), 'idle-chrome');
	await flush();
	const playing = page(fakeLocks(), 'agent-pane', { playing: true });
	await flush();
	await run(12, idle, playing);
	assert.equal(engine.lease.holder, 'idle-chrome');
});

test('same browser: a playing follower takes the lock from an idle leader', async () => {
	const locks = fakeLocks();
	const idle = page(locks, 'tab-a');
	await flush();
	const playing = page(locks, 'tab-b', { playing: true });
	await flush();
	await run(4, idle, playing);
	assert.equal(playing.role(), 'leader');
	assert.equal(idle.role(), 'follower');
	assert.equal(engine.lease.holder, 'tab-b');
});

test('a hidden idle leader yields to a visible tab in another browser', async () => {
	const hidden = page(fakeLocks(), 'backgrounded', { visible: false });
	await flush();
	const visible = page(fakeLocks(), 'front');
	await flush();
	await run(4, hidden, visible);
	assert.equal(engine.lease.holder, 'front');
	assert.equal(visible.role(), 'leader');
});

test('a gesture takes control from an idle holder, never from a playing one', async () => {
	const holder = page(fakeLocks(), 'holder');
	await flush();
	const touched = page(fakeLocks(), 'touched');
	await flush();
	await run(3, holder, touched);
	assert.equal(touched.role(), 'follower', 'precondition');
	holder.tabState.playing = true;
	touched.mirror.noteGesture();
	await run(3, holder, touched);
	assert.equal(engine.lease.holder, 'holder', 'the set keeps playing where it is');
	holder.tabState.playing = false;
	touched.mirror.noteGesture();
	await run(3, holder, touched);
	assert.equal(engine.lease.holder, 'touched');
	assert.equal(engine.requests.filter((r) => r.takeover === '1').length, 1);
});

test('a hidden, silent follower makes no request at all', async () => {
	const leader = page(fakeLocks(), 'leader');
	await flush();
	const quiet = page(fakeLocks(), 'quiet', { visible: false });
	await flush();
	const before = engine.requests.length;
	await run(6, quiet);
	assert.equal(engine.requests.length - before, 0);
	assert.ok(leader);
});

// ------------------------------------------ confirmed by the engine ---

test('a lock holder is not a confirmed leader until the engine accepts its PUT', async () => {
	const a = page(fakeLocks(), 'a');
	await flush();
	assert.equal(a.leadership.isConfirmedLeader(), true, 'its promotion PUT was accepted');
	const b = page(fakeLocks(), 'b');
	await flush();
	assert.equal(b.leadership.holdsLocalLock(), true);
	assert.equal(b.leadership.isConfirmedLeader(), false, 'refused by the lease: never confirmed');
	a.tabState.playing = false;
	b.tabState.playing = true;
	await run(3, a, b);
	assert.equal(b.leadership.isConfirmedLeader(), true);
	assert.equal(a.leadership.isConfirmedLeader(), false, 'demoted: no longer confirmed');
});

test('a tab refused by the lease never opens the leader-only gate, not even briefly', async () => {
	const a = page(fakeLocks(), 'a');
	await flush();
	const b = page(fakeLocks(), 'b');
	let installs = 0;
	whileLeader(confirmedLeadership(b.leadership), () => {
		installs += 1;
		return () => {};
	});
	await flush();
	await run(3, a, b);
	assert.equal(b.leadership.holdsLocalLock(), true, 'precondition: b holds its own browser lock');
	assert.equal(installs, 0, 'restore and shared writers never ran in the refused tab');
	assert.ok(a);
});

test('bug #31: Take control from a playing leader in another browser sticks', async () => {
	const playing = page(fakeLocks(), 'core-pane', { playing: true });
	await flush();
	const chrome = page(fakeLocks(), 'maintainer-chrome');
	await flush();
	assert.equal(chrome.role(), 'follower', 'precondition: the playing pane leads');
	chrome.leadership.takeControl();
	await flush();
	// The old leader is still playing until its silencer runs; it must not win it back.
	await run(4, playing, chrome);
	assert.equal(engine.lease.holder, 'maintainer-chrome');
	assert.equal(playing.role(), 'follower');
	assert.equal(chrome.leadership.isConfirmedLeader(), true);
});
