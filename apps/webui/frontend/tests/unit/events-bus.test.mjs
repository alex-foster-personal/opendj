/**
 * Contract tests for the WS invalidation bus (src/lib/api/events-bus.ts).
 *
 * The bus is driven entirely through injected seams -- a fake socket and a fake
 * scheduler -- so every assertion here is synchronous and there is not a single
 * real sleep or real socket in the file. Backoff is asserted by reading the
 * delays the bus ASKED for, which is the actual contract; sleeping to observe
 * them would test the platform's timer instead.
 */
import assert from 'node:assert/strict';
import { before, beforeEach, afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const WS_URL = 'ws://events.example.test/api/v1/events';

let bus;
let sockets;
let scheduler;
let errors;
let originalConsoleError;

/** Minimal stand-in for the browser WebSocket: the bus only ever assigns the
 * four handlers and calls close(). Test helpers drive the server side. */
class FakeSocket {
	constructor(url) {
		this.url = url;
		this.onopen = null;
		this.onmessage = null;
		this.onclose = null;
		this.onerror = null;
		this.closedByClient = false;
	}

	/** Server accepted the connection. */
	open() {
		this.onopen?.({});
	}

	/** Deliver one well-formed envelope. */
	deliver(topic, seq, payload) {
		this.onmessage?.({
			data: JSON.stringify({ topic, seq, ts: '2026-08-19T10:00:00.000Z', payload })
		});
	}

	/** Deliver whatever bytes the test wants, contract or not. */
	deliverRaw(data) {
		this.onmessage?.({ data });
	}

	/** The hello frame the engine always sends first. */
	hello({ contractRev = 'rev-1', engineVersion = '1.0.0', seqStart = 0, topics = [] } = {}) {
		this.deliver('hello', seqStart, {
			contract_rev: contractRev,
			engine_version: engineVersion,
			seq_start: seqStart,
			topics
		});
	}

	/** Server closed the socket. 1013 is the slow-consumer close. */
	serverClose(code = 1006, reason = '') {
		this.onclose?.({ code, reason });
	}

	close() {
		this.closedByClient = true;
	}
}

function makeScheduler() {
	const pending = [];
	let nextId = 1;
	return {
		/** Every delay the bus has asked for, in order. This is the backoff. */
		delays: [],
		setTimeout(handler, ms) {
			const id = nextId;
			nextId += 1;
			this.delays.push(ms);
			pending.push({ id, handler });
			return id;
		},
		clearTimeout(id) {
			const index = pending.findIndex((entry) => entry.id === id);
			if (index !== -1) pending.splice(index, 1);
		},
		/** Fire everything currently due, as a fake clock tick would. */
		tick() {
			const due = pending.splice(0, pending.length);
			for (const entry of due) entry.handler();
		},
		get pendingCount() {
			return pending.length;
		}
	};
}

/** Open the bus and return the live fake socket, hello already delivered. */
function connectAndHello(helloOptions) {
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	const socket = sockets.at(-1);
	socket.open();
	socket.hello(helloOptions);
	return socket;
}

function _factory(url) {
	const socket = new FakeSocket(url);
	sockets.push(socket);
	return socket;
}

before(async () => {
	bus = await loadTypeScriptModule('src/lib/api/events-bus.ts');
});

beforeEach(() => {
	sockets = [];
	scheduler = makeScheduler();
	errors = [];
	originalConsoleError = console.error;
	// The bus fails loud on every off-contract frame. Capturing rather than
	// silencing keeps that observable: tests assert the error was raised.
	console.error = (...args) => errors.push(args.map(String).join(' '));
	bus._resetForTests();
});

afterEach(() => {
	console.error = originalConsoleError;
	bus._resetForTests();
});

// ------------------------------------------------------------------- hello

test('the hello frame is parsed and its contract fields are exposed', () => {
	connectAndHello({ contractRev: 'rev-7', engineVersion: '2.3.1', seqStart: 41 });

	const hello = bus.getHello();
	assert.equal(hello.contract_rev, 'rev-7');
	assert.equal(hello.engine_version, '2.3.1');
	assert.equal(hello.seq_start, 41);
	assert.equal(bus.getLastSeq(), 41, 'seq_start seeds the gap baseline');
	assert.deepEqual(errors, []);
});

test('a hello carrying the wrong types is an error and a resync, not a silent skip', () => {
	const seen = [];
	bus.subscribeResync((reason) => seen.push(reason));
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	const socket = sockets.at(-1);
	socket.open();

	socket.deliver('hello', 0, { contract_rev: 'rev-1', engine_version: 3, seq_start: 0, topics: [] });

	assert.deepEqual(seen, ['malformed']);
	assert.equal(bus.getHello(), null);
	assert.equal(errors.length, 1);
	assert.match(errors[0], /hello lacks contract_rev\/engine_version/);
});

test('the engine advertising a new contract_rev on reconnect is reported loudly', () => {
	const socket = connectAndHello({ contractRev: 'rev-1' });
	socket.serverClose();
	scheduler.tick();
	const reconnected = sockets.at(-1);
	reconnected.open();
	reconnected.hello({ contractRev: 'rev-2' });

	assert.equal(bus.getHello().contract_rev, 'rev-2');
	assert.equal(errors.length, 1);
	assert.match(errors[0], /contract_rev changed rev-1 -> rev-2/);
});

// --------------------------------------------------------------- seq gaps

test('a contiguous seq run delivers events and never fires resync', () => {
	const resyncs = [];
	const seen = [];
	bus.subscribeResync((reason) => resyncs.push(reason));
	bus.subscribe('jobs.updated', (envelope) => seen.push(envelope.seq));
	const socket = connectAndHello({ seqStart: 10 });

	socket.deliver('jobs.updated', 11, {});
	socket.deliver('jobs.updated', 12, {});
	socket.deliver('jobs.updated', 13, {});

	assert.deepEqual(seen, [11, 12, 13]);
	assert.deepEqual(resyncs, [], 'no gap, so nothing to resync');
	assert.equal(bus.getLastSeq(), 13);
});

test('a skipped seq fires one resync and still delivers the frame that revealed it', () => {
	const resyncs = [];
	const seen = [];
	bus.subscribeResync((reason) => resyncs.push(reason));
	bus.subscribe('jobs.updated', (envelope) => seen.push(envelope.seq));
	const socket = connectAndHello({ seqStart: 4 });

	socket.deliver('jobs.updated', 5, {});
	socket.deliver('jobs.updated', 9, {}); // 6, 7 and 8 were missed.
	socket.deliver('jobs.updated', 10, {});

	assert.deepEqual(resyncs, ['gap']);
	assert.deepEqual(seen, [5, 9, 10], 'the gap does not swallow the frame');
	assert.equal(bus.getLastSeq(), 10, 'the baseline resets to the new seq, so 10 is contiguous');
	assert.match(errors[0], /seq gap: expected 6, got 9/);
});

test('a seq that rewinds is a gap too, because the engine restarted its counter', () => {
	const resyncs = [];
	bus.subscribeResync((reason) => resyncs.push(reason));
	const socket = connectAndHello({ seqStart: 900 });

	socket.deliver('jobs.updated', 1, {});

	assert.deepEqual(resyncs, ['gap']);
});

test('a malformed frame is treated as a gap, since its contents are unknowable', () => {
	const resyncs = [];
	bus.subscribeResync((reason) => resyncs.push(reason));
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliverRaw('{ not json');
	socket.deliverRaw(JSON.stringify({ topic: 'jobs.updated', seq: 'one', ts: 'x', payload: {} }));
	socket.deliverRaw(JSON.stringify({ topic: 'jobs.updated', seq: 2, ts: 'x', payload: null }));

	assert.deepEqual(resyncs, ['malformed', 'malformed', 'malformed']);
	assert.equal(errors.length, 3);
	assert.match(errors[0], /not JSON/);
	assert.match(errors[1], /non integer seq/);
	assert.match(errors[2], /non object payload/);
});

test('a data frame arriving before the hello is a contract break and a gap', () => {
	const resyncs = [];
	const seen = [];
	bus.subscribeResync((reason) => resyncs.push(reason));
	bus.subscribe('jobs.updated', (envelope) => seen.push(envelope.seq));
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	const socket = sockets.at(-1);
	socket.open();

	socket.deliver('jobs.updated', 3, {});

	assert.deepEqual(resyncs, ['gap']);
	assert.deepEqual(seen, [3]);
	assert.match(errors[0], /arrived before the hello frame/);
});

// ----------------------------------------------------- subscribe/unsubscribe

test('subscribe delivers only its own topic and the handle detaches it', () => {
	const jobs = [];
	const healthEvents = [];
	const unsubscribeJobs = bus.subscribe('jobs.updated', (envelope) => jobs.push(envelope.seq));
	bus.subscribe('health.changed', (envelope) => healthEvents.push(envelope.seq));
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliver('jobs.updated', 1, {});
	socket.deliver('health.changed', 2, {});
	unsubscribeJobs();
	socket.deliver('jobs.updated', 3, {});
	socket.deliver('health.changed', 4, {});

	assert.deepEqual(jobs, [1], 'the unsubscribed listener stops receiving');
	assert.deepEqual(healthEvents, [2, 4]);
});

test('unsubscribing twice is harmless and leaves other listeners on the topic', () => {
	const first = [];
	const second = [];
	const unsubscribeFirst = bus.subscribe('jobs.updated', (e) => first.push(e.seq));
	bus.subscribe('jobs.updated', (e) => second.push(e.seq));
	const socket = connectAndHello({ seqStart: 0 });

	unsubscribeFirst();
	unsubscribeFirst();
	socket.deliver('jobs.updated', 1, {});

	assert.deepEqual(first, []);
	assert.deepEqual(second, [1]);
});

test('a listener that unsubscribes while being notified does not disturb the fan-out', () => {
	const order = [];
	const unsubscribe = bus.subscribe('jobs.updated', (e) => {
		order.push(`a${e.seq}`);
		unsubscribe();
	});
	bus.subscribe('jobs.updated', (e) => order.push(`b${e.seq}`));
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliver('jobs.updated', 1, {});
	socket.deliver('jobs.updated', 2, {});

	assert.deepEqual(order, ['a1', 'b1', 'b2']);
});

// ------------------------------------------------------------ kind filtering

test('subscribeKind receives only its own library.changed kind, with the ids lifted out', () => {
	const trackIds = [];
	const playlistIds = [];
	bus.subscribeKind('tracks', (ids) => trackIds.push(...ids));
	bus.subscribeKind('playlists', (ids) => playlistIds.push(...ids));
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliver('library.changed', 1, { kind: 'tracks', ids: ['t1', 't2'] });
	socket.deliver('library.changed', 2, { kind: 'playlists', ids: ['p1'] });
	socket.deliver('library.changed', 3, { kind: 'mytags', ids: ['m1'] });

	assert.deepEqual(trackIds, ['t1', 't2']);
	assert.deepEqual(playlistIds, ['p1']);
	assert.deepEqual(errors, [], 'mytags is a known kind with no subscriber, not an error');
});

test('a kind subscriber also sees the envelope, and its handle detaches it', () => {
	const seen = [];
	const unsubscribe = bus.subscribeKind('tracks', (ids, envelope) =>
		seen.push([envelope.seq, ...ids])
	);
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliver('library.changed', 1, { kind: 'tracks', ids: ['t1'] });
	unsubscribe();
	socket.deliver('library.changed', 2, { kind: 'tracks', ids: ['t2'] });

	assert.deepEqual(seen, [[1, 't1']]);
});

test('topic subscribers on library.changed see every kind', () => {
	const kinds = [];
	bus.subscribe('library.changed', (envelope) => kinds.push(envelope.payload.kind));
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliver('library.changed', 1, { kind: 'tracks', ids: [] });
	socket.deliver('library.changed', 2, { kind: 'dedup', ids: ['c9'] });

	assert.deepEqual(kinds, ['tracks', 'dedup']);
});

test('an unknown kind is reported as contract drift and reaches no kind subscriber', () => {
	const trackIds = [];
	bus.subscribeKind('tracks', (ids) => trackIds.push(...ids));
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliver('library.changed', 1, { kind: 'sparklines', ids: ['x'] });

	assert.deepEqual(trackIds, []);
	assert.match(errors[0], /unknown kind 'sparklines'/);
});

test('a library.changed with malformed ids is an error, not a crash', () => {
	const trackIds = [];
	bus.subscribeKind('tracks', (ids) => trackIds.push(...ids));
	const socket = connectAndHello({ seqStart: 0 });

	socket.deliver('library.changed', 1, { kind: 'tracks', ids: 'not-an-array' });

	assert.deepEqual(trackIds, []);
	assert.match(errors[0], /non string\[\] ids/);
});

// -------------------------------------------------------- reconnect/backoff

test('connection state walks connecting to open to closed', () => {
	const states = [];
	bus.subscribeConnectionState((state) => states.push(state));

	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	assert.equal(bus.getConnectionState(), 'connecting');
	sockets.at(-1).open();
	assert.equal(bus.getConnectionState(), 'open');
	sockets.at(-1).serverClose();
	assert.equal(bus.getConnectionState(), 'closed');

	assert.deepEqual(states, ['connecting', 'open', 'closed']);
});

test('backoff doubles from 500ms and caps at 10s, retrying forever', () => {
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	sockets.at(-1).open();

	// The daemon stays down: every retry is refused, so nothing ever reopens
	// and the delay keeps escalating. (A retry that DOES open resets it, which
	// is the next test.)
	for (let attempt = 0; attempt < 8; attempt += 1) {
		sockets.at(-1).serverClose();
		scheduler.tick();
	}

	assert.deepEqual(scheduler.delays, [500, 1000, 2000, 4000, 8000, 10000, 10000, 10000]);
	assert.equal(sockets.length, 9, 'one initial socket plus one per retry');
	assert.equal(bus.getConnectionState(), 'connecting', 'still trying, never gives up');
});

test('a successful open resets the backoff, so a later blip starts at 500ms again', () => {
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	sockets.at(-1).open();
	sockets.at(-1).serverClose();
	scheduler.tick();
	sockets.at(-1).serverClose(); // Retry never opened: backoff keeps climbing.
	scheduler.tick();
	sockets.at(-1).open(); // This one sticks.
	sockets.at(-1).serverClose();

	assert.deepEqual(scheduler.delays, [500, 1000, 500]);
});

test('every reconnect fires resync, but the first connect does not', () => {
	const resyncs = [];
	bus.subscribeResync((reason) => resyncs.push(reason));

	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	sockets.at(-1).open();
	assert.deepEqual(resyncs, [], 'nothing could have been missed before the first connection');

	sockets.at(-1).serverClose();
	scheduler.tick();
	sockets.at(-1).open();

	assert.deepEqual(resyncs, ['reconnect']);
});

test('a 1013 slow-consumer close resyncs at the close and again on reopen', () => {
	const resyncs = [];
	bus.subscribeResync((reason) => resyncs.push(reason));
	const socket = connectAndHello({ seqStart: 0 });

	socket.serverClose(1013, 'slow consumer');
	scheduler.tick();
	sockets.at(-1).open();

	// Two invalidations, both real: one for the frames that overflowed the
	// queue, one for whatever was published during the reconnect window.
	assert.deepEqual(resyncs, ['slow-consumer', 'reconnect']);
	assert.match(errors[0], /closed as a slow consumer \(1013 slow consumer\)/);
});

test('the seq baseline is dropped across a reconnect so the new hello is authoritative', () => {
	const resyncs = [];
	const socket = connectAndHello({ seqStart: 500 });
	bus.subscribeResync((reason) => resyncs.push(reason));

	socket.serverClose();
	assert.equal(bus.getLastSeq(), null, 'a dead engine counter is not a baseline');
	scheduler.tick();
	const reconnected = sockets.at(-1);
	reconnected.open();
	reconnected.hello({ seqStart: 0 }); // Engine restarted from zero.
	reconnected.deliver('jobs.updated', 1, {});

	assert.deepEqual(resyncs, ['reconnect'], 'the restart is not double-counted as a gap');
	assert.equal(bus.getLastSeq(), 1);
});

// ---------------------------------------------------------------- lifecycle

test('connect is idempotent, so mounting twice does not open a second socket', () => {
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	sockets.at(-1).open();
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });

	assert.equal(sockets.length, 1);
});

test('disconnect closes the socket and stops retrying', () => {
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	const socket = sockets.at(-1);
	socket.open();

	bus.disconnect();

	assert.equal(socket.closedByClient, true);
	assert.equal(bus.getConnectionState(), 'closed');
	assert.equal(scheduler.pendingCount, 0, 'a deliberate close is not a reconnect trigger');
	assert.equal(sockets.length, 1);
});

test('a socket that closes after being replaced cannot drive the live connection', () => {
	bus.connect(WS_URL, { socketFactory: _factory, scheduler });
	const stale = sockets.at(-1);
	stale.serverClose();
	scheduler.tick();
	const live = sockets.at(-1);
	live.open();

	const resyncs = [];
	bus.subscribeResync((reason) => resyncs.push(reason));
	stale.serverClose(1013, 'slow consumer');
	stale.deliver('library.changed', 99, { kind: 'tracks', ids: ['x'] });

	assert.deepEqual(resyncs, [], 'the abandoned socket is inert');
	assert.equal(bus.getConnectionState(), 'open');
});
