/**
 * The frontend end of the engine's WebSocket INVALIDATION BUS (`/api/v1/events`).
 *
 * The socket carries NO data the UI renders. It carries "something changed,
 * refetch it over HTTP". That inversion is the whole design and it drives every
 * decision below:
 *
 * 1. `seq` is a single engine-lifetime counter across ALL topics, not a
 *    per-topic one. A client that sees `seq` skip knows only that it missed
 *    something, never what. There is no replay and no resume cursor (a
 *    documented non-goal in `apps/engine_core/ws.py`), so the only correct
 *    response to a gap is to invalidate everything and refetch.
 * 2. Therefore a MISSED invalidation is a correctness bug (the UI shows stale
 *    data and never learns better) while a DUPLICATE invalidation costs one
 *    extra HTTP round trip. Every ambiguous case in this module resolves toward
 *    firing resync. That is why a 1013 close fires resync at close time AND
 *    again when the socket reopens: events published during the reconnect
 *    window are missed too, and one redundant refetch is the cheap error.
 * 3. A malformed frame is treated as a gap, not skipped. We cannot know what
 *    the frame would have invalidated, so the safe reading of an unparseable
 *    frame is "you missed an event". It is also logged as an error, because a
 *    frame this end cannot parse is a contract break someone must fix.
 *
 * Consumers subscribe by topic (`subscribe`), by `library.changed` kind
 * (`subscribeKind`), or to the invalidate-everything signal (`subscribeResync`).
 * A consumer that handles a kind MUST also handle resync, otherwise it goes
 * stale exactly when the bus is least reliable.
 */

import { API_BASE } from './base';

export const EVENTS_PATH = '/api/v1/events';

/** The engine closes a client that cannot keep up rather than dropping frames
 * silently. See `SLOW_CONSUMER_CODE` in `apps/engine_core/ws.py`. */
export const SLOW_CONSUMER_CODE = 1013;

export const TOPIC_HELLO = 'hello';
export const TOPIC_LIBRARY_CHANGED = 'library.changed';
export const TOPIC_JOBS_UPDATED = 'jobs.updated';
export const TOPIC_HEALTH_CHANGED = 'health.changed';
export const TOPIC_SHELL_QUIT = 'shell.quit';

/** `kind` values the server publishes on `library.changed`. Mirrors the
 * `publish("library.changed", {"kind": ...})` call sites under
 * `apps/webui/server/routes/`. */
export const LIBRARY_KINDS = [
	'tracks',
	'playlists',
	'mytags',
	'hot_cues',
	'smartlists',
	'dedup',
	'reconcile',
	'ui_prefs',
	'pairings',
	'library_jobs',
	'playlist_sets',
	'midi_maps'
] as const;

export type LibraryKind = (typeof LIBRARY_KINDS)[number];

/** Backoff floor and ceiling. Doubling from 500ms caps at 10s, so a daemon that
 * is down for an hour is retried roughly every 10s rather than never. */
const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 10_000;

export type ConnectionState = 'connecting' | 'open' | 'closed';

/** Why the bus is telling subscribers to throw away what they have.
 * `slow-consumer` is distinguished from `gap` because it means THIS client was
 * too slow, which is actionable in a way that a server restart is not.
 * `initial-connect` is distinguished from `reconnect` for the same reason:
 * both mean "something might have changed since the last time you asked",
 * but only one of them follows a period where this client had a live
 * connection at all. */
export type ResyncReason = 'gap' | 'reconnect' | 'slow-consumer' | 'malformed' | 'initial-connect';

export interface EventEnvelope {
	topic: string;
	seq: number;
	ts: string;
	payload: Record<string, unknown>;
}

export interface HelloPayload {
	contract_rev: string;
	engine_version: string;
	seq_start: number;
	topics: string[];
}

export interface LibraryChangedPayload {
	kind: string;
	ids: string[];
}

/** The slice of `WebSocket` this module uses. Narrowed to an interface so unit
 * tests can inject a fake without a DOM and without real sockets. */
export interface WebSocketLike {
	onopen: ((event: unknown) => void) | null;
	onmessage: ((event: { data: unknown }) => void) | null;
	onclose: ((event: { code: number; reason: string }) => void) | null;
	onerror: ((event: unknown) => void) | null;
	close(): void;
}

/** Timer seam, so backoff is asserted by stepping a fake clock instead of by
 * sleeping. Real timers are the default. */
export interface Scheduler {
	setTimeout(handler: () => void, ms: number): number;
	clearTimeout(id: number): void;
}

export interface ConnectOptions {
	socketFactory?: (url: string) => WebSocketLike;
	scheduler?: Scheduler;
}

export type EnvelopeListener = (envelope: EventEnvelope) => void;
export type KindListener = (ids: string[], envelope: EventEnvelope) => void;
export type ResyncListener = (reason: ResyncReason) => void;
export type ConnectionStateListener = (state: ConnectionState) => void;

/** Handle returned by every `subscribe*` call. Calling it twice is harmless. */
export type Unsubscribe = () => void;

// ------------------------------------------------------------------ registry

const _topicListeners = new Map<string, Set<EnvelopeListener>>();
const _kindListeners = new Map<string, Set<KindListener>>();
const _resyncListeners = new Set<ResyncListener>();
const _connectionListeners = new Set<ConnectionStateListener>();

function _addTo<T>(registry: Map<string, Set<T>>, key: string, listener: T): Unsubscribe {
	let bucket = registry.get(key);
	if (bucket === undefined) {
		bucket = new Set<T>();
		registry.set(key, bucket);
	}
	bucket.add(listener);
	return () => {
		const current = registry.get(key);
		if (current === undefined) return;
		current.delete(listener);
		if (current.size === 0) registry.delete(key);
	};
}

/** Every event on one topic, envelope included. */
export function subscribe(topic: string, listener: EnvelopeListener): Unsubscribe {
	return _addTo(_topicListeners, topic, listener);
}

/** Only `library.changed` events whose payload `kind` matches, with the changed
 * ids lifted out. The common consumer shape. */
export function subscribeKind(kind: LibraryKind, listener: KindListener): Unsubscribe {
	return _addTo(_kindListeners, kind, listener);
}

/** "Throw away everything you cached and refetch." Fires on a seq gap, on a
 * malformed frame, on a slow-consumer close, and on every reconnect. */
export function subscribeResync(listener: ResyncListener): Unsubscribe {
	_resyncListeners.add(listener);
	return () => {
		_resyncListeners.delete(listener);
	};
}

/** Connection state changes, for a status dot. Not fired for no-op transitions. */
export function subscribeConnectionState(listener: ConnectionStateListener): Unsubscribe {
	_connectionListeners.add(listener);
	return () => {
		_connectionListeners.delete(listener);
	};
}

// --------------------------------------------------------------------- state

let _state: ConnectionState = 'closed';
let _hello: HelloPayload | null = null;
let _lastSeq: number | null = null;
let _socket: WebSocketLike | null = null;
let _url: string | null = null;
let _socketFactory: ((url: string) => WebSocketLike) | null = null;
let _scheduler: Scheduler | null = null;
let _retryDelayMs = BACKOFF_BASE_MS;
let _retryTimer: number | null = null;
/** False until the first successful open, so a resync fired there is reported
 * as `initial-connect` rather than `reconnect` (see `ResyncReason`). Both
 * fire a resync: the socket connects asynchronously, after a capability-probe
 * round trip, well after any boot-time HTTP call a consumer already made, so
 * a change landing in that window is exactly as invisible as a reconnect
 * gap. PR #1656 review round 5 found two independent consumers (a cached
 * health read in api.ts, a library refresh trigger in BrowserPanel.svelte)
 * that had each separately discovered this gap and worked around it with
 * their own extra subscription; fixed at the source instead so every current
 * and future `subscribeResync` consumer gets it for free. */
let _hasConnected = false;
/** Set while `disconnect()` is tearing down, so the close handler does not
 * schedule a reconnect for a socket we closed on purpose. */
let _shuttingDown = false;

export function getConnectionState(): ConnectionState {
	return _state;
}

/** The hello frame's contents, or null before the first hello lands. */
export function getHello(): HelloPayload | null {
	return _hello;
}

/** Last `seq` observed on this engine lifetime, or null before the hello. */
export function getLastSeq(): number | null {
	return _lastSeq;
}

/** Current reconnect delay. Exported so a status surface (and the unit tests)
 * can see the backoff without waiting for it. */
export function getRetryDelayMs(): number {
	return _retryDelayMs;
}

// ------------------------------------------------------------------ dispatch

/** Call one subscriber in isolation.
 *
 * The ONLY deliberate swallow in this module, and it earns its keep: the bus
 * fans one frame out to unrelated components, so a single throwing subscriber
 * would otherwise starve every subscriber registered after it and lose the
 * frame entirely. Since a missed invalidation is the expensive failure here
 * (permanently stale UI) and a noisy log is the cheap one, the throw is
 * contained and reported rather than allowed to take the fan-out down. */
function _callListener(label: string, invoke: () => void): void {
	try {
		invoke();
	} catch (exc) {
		console.error(`[events-bus] a ${label} listener threw, continuing the fan-out: ${String(exc)}`);
	}
}

function _setState(next: ConnectionState): void {
	if (_state === next) return;
	_state = next;
	for (const listener of [..._connectionListeners]) {
		_callListener('connection-state', () => listener(next));
	}
}

function _fireResync(reason: ResyncReason): void {
	// Copied before iterating: a resync handler that unsubscribes (a component
	// tearing down mid-refetch) must not mutate the set being walked.
	for (const listener of [..._resyncListeners]) {
		_callListener('resync', () => listener(reason));
	}
}

function _fireEnvelope(envelope: EventEnvelope): void {
	const bucket = _topicListeners.get(envelope.topic);
	if (bucket !== undefined) {
		for (const listener of [...bucket]) {
			_callListener(`topic=${envelope.topic}`, () => listener(envelope));
		}
	}
	if (envelope.topic !== TOPIC_LIBRARY_CHANGED) return;
	const change = _readLibraryChanged(envelope.payload);
	if (change === null) return;
	const kindBucket = _kindListeners.get(change.kind);
	if (kindBucket === undefined) return;
	for (const listener of [...kindBucket]) {
		_callListener(`kind=${change.kind}`, () => listener(change.ids, envelope));
	}
}

/** Decode a `library.changed` payload, or null when it does not match the
 * contract. An unknown `kind` is contract drift (the server grew a kind this
 * build does not know) so it is logged rather than swallowed, but it is not a
 * gap: nothing was missed, this client simply has no consumer for it. */
function _readLibraryChanged(payload: Record<string, unknown>): LibraryChangedPayload | null {
	const kind = payload.kind;
	const ids = payload.ids;
	if (typeof kind !== 'string') {
		console.error('[events-bus] library.changed payload has no string kind', payload);
		return null;
	}
	if (!Array.isArray(ids) || !ids.every((id) => typeof id === 'string')) {
		console.error(`[events-bus] library.changed kind=${kind} has a non string[] ids`, payload);
		return null;
	}
	if (!(LIBRARY_KINDS as readonly string[]).includes(kind)) {
		console.error(`[events-bus] library.changed carries unknown kind '${kind}'`);
		return null;
	}
	return { kind, ids };
}

// -------------------------------------------------------------------- frames

/** Parse and shape-check one frame. Returns null (never throws) for anything
 * off-contract, having logged what was wrong; the caller turns that into a
 * gap. */
function _parseEnvelope(raw: unknown): EventEnvelope | null {
	if (typeof raw !== 'string') {
		console.error(`[events-bus] frame is ${typeof raw}, expected a JSON string`);
		return null;
	}
	let decoded: unknown;
	try {
		decoded = JSON.parse(raw) as unknown;
	} catch (exc) {
		console.error(`[events-bus] frame is not JSON: ${String(exc)}`);
		return null;
	}
	if (typeof decoded !== 'object' || decoded === null || Array.isArray(decoded)) {
		console.error('[events-bus] frame is not a JSON object', decoded);
		return null;
	}
	const frame = decoded as Record<string, unknown>;
	const { topic, seq, ts, payload } = frame;
	if (typeof topic !== 'string' || topic === '') {
		console.error('[events-bus] frame has no topic', frame);
		return null;
	}
	if (typeof seq !== 'number' || !Number.isInteger(seq) || seq < 0) {
		console.error(`[events-bus] frame topic=${topic} has a non integer seq`, frame);
		return null;
	}
	if (typeof ts !== 'string') {
		console.error(`[events-bus] frame topic=${topic} has a non string ts`, frame);
		return null;
	}
	if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
		console.error(`[events-bus] frame topic=${topic} has a non object payload`, frame);
		return null;
	}
	return { topic, seq, ts, payload: payload as Record<string, unknown> };
}

function _readHello(payload: Record<string, unknown>): HelloPayload | null {
	const { contract_rev, engine_version, seq_start, topics } = payload;
	if (typeof contract_rev !== 'string' || typeof engine_version !== 'string') {
		console.error('[events-bus] hello lacks contract_rev/engine_version', payload);
		return null;
	}
	if (typeof seq_start !== 'number' || !Number.isInteger(seq_start) || seq_start < 0) {
		console.error('[events-bus] hello has a non integer seq_start', payload);
		return null;
	}
	if (!Array.isArray(topics) || !topics.every((t) => typeof t === 'string')) {
		console.error('[events-bus] hello topics is not a string[]', payload);
		return null;
	}
	return { contract_rev, engine_version, seq_start, topics };
}

function _onFrame(raw: unknown): void {
	const envelope = _parseEnvelope(raw);
	if (envelope === null) {
		// We cannot know what this frame carried, so the honest reading is
		// "an event was missed".
		_fireResync('malformed');
		return;
	}
	if (envelope.topic === TOPIC_HELLO) {
		_onHello(envelope);
		return;
	}
	if (_lastSeq === null) {
		// A data frame before the hello: the engine always sends hello first,
		// so this is a contract break, and the seq baseline is unknown.
		console.error(`[events-bus] topic=${envelope.topic} arrived before the hello frame`);
		_lastSeq = envelope.seq;
		_fireResync('gap');
		_fireEnvelope(envelope);
		return;
	}
	const expected = _lastSeq + 1;
	_lastSeq = envelope.seq;
	if (envelope.seq !== expected) {
		// Covers both directions: a skip means dropped events, and a rewind
		// means the engine restarted and its counter went back to zero. Both
		// invalidate everything this client holds.
		console.error(`[events-bus] seq gap: expected ${expected}, got ${envelope.seq}`);
		_fireResync('gap');
	}
	_fireEnvelope(envelope);
}

function _onHello(envelope: EventEnvelope): void {
	const hello = _readHello(envelope.payload);
	if (hello === null) {
		_fireResync('malformed');
		return;
	}
	const previous = _hello;
	_hello = hello;
	_lastSeq = hello.seq_start;
	if (previous !== null && previous.contract_rev !== hello.contract_rev) {
		console.error(
			`[events-bus] contract_rev changed ${previous.contract_rev} -> ${hello.contract_rev}; ` +
				'this build was generated against the old one, reload the UI'
		);
	}
}

// ---------------------------------------------------------------- connection

function _resolveUrl(url?: string): string {
	if (url !== undefined) return url;
	const base = API_BASE === '' ? _windowOrigin() : API_BASE;
	const resolved = new URL(EVENTS_PATH, base);
	resolved.protocol = resolved.protocol === 'https:' ? 'wss:' : 'ws:';
	return resolved.toString();
}

function _windowOrigin(): string {
	if (typeof window === 'undefined') {
		throw new Error(
			'events-bus: API_BASE is same origin but there is no window to read it from; ' +
				'pass an explicit url to connect()'
		);
	}
	return window.location.origin;
}

function _defaultSocketFactory(url: string): WebSocketLike {
	if (typeof WebSocket === 'undefined') {
		throw new Error('events-bus: no WebSocket in this runtime; pass options.socketFactory');
	}
	return new WebSocket(url) as unknown as WebSocketLike;
}

const _defaultScheduler: Scheduler = {
	setTimeout: (handler, ms) => setTimeout(handler, ms) as unknown as number,
	clearTimeout: (id) => clearTimeout(id)
};

/**
 * Open the bus and keep it open. Idempotent: calling it while already
 * connecting, open, or WAITING TO RETRY is a no-op, so mounting twice (HMR, a
 * remount) does not open a second socket.
 *
 * The retry-armed window matters as much as the live-socket one. Between a
 * close and its scheduled reopen `_socket` is null but the bus is still very
 * much alive, so guarding on `_socket` alone let a remount open a socket that
 * the pending timer then immediately orphaned, unclosed and unreferenced. It
 * would also have collapsed the backoff to zero, which is exactly the wrong
 * behaviour against a daemon that is down.
 */
export function connect(url?: string, options: ConnectOptions = {}): void {
	if (_socket !== null || _retryTimer !== null) return;
	_url = _resolveUrl(url);
	_socketFactory = options.socketFactory ?? _defaultSocketFactory;
	_scheduler = options.scheduler ?? _defaultScheduler;
	_shuttingDown = false;
	_open();
}

function _open(): void {
	if (_url === null || _socketFactory === null) {
		throw new Error('events-bus: _open() before connect() configured the url');
	}
	_setState('connecting');
	let socket: WebSocketLike;
	try {
		socket = _socketFactory(_url);
	} catch (exc) {
		// The WebSocket constructor throws SYNCHRONOUSLY on a SecurityError
		// (ws:// from an https page) or a SyntaxError (malformed url), so there
		// is no socket to deliver a close event and nothing arms a retry. Left
		// alone the throw escapes connect(), the bus stays pinned in
		// 'connecting' and never reconnects. It is just a failed attempt: state
		// closed, normal backoff, try again.
		console.error(`[events-bus] opening ${_url} threw: ${String(exc)}`);
		_socket = null;
		_setState('closed');
		if (!_shuttingDown) _scheduleReconnect();
		return;
	}
	_socket = socket;
	socket.onopen = () => {
		if (_socket !== socket) return;
		_retryDelayMs = BACKOFF_BASE_MS;
		_setState('open');
		// Anything published before this open is gone: no replay, so the only
		// sound assumption is that we missed something. True on a reconnect
		// (we were away) and equally true on the first-ever connect (the
		// socket took time to open, and nothing before it had a subscriber).
		_fireResync(_hasConnected ? 'reconnect' : 'initial-connect');
		_hasConnected = true;
	};
	socket.onmessage = (event) => {
		if (_socket !== socket) return;
		_onFrame(event.data);
	};
	socket.onerror = () => {
		// Deliberately quiet. The browser fires `error` immediately before
		// `close` on a refused connection, and a daemon that is not running yet
		// is the normal case during development; `close` does the real work.
	};
	socket.onclose = (event) => {
		if (_socket !== socket) return;
		_socket = null;
		// The seq baseline belongs to a connection. Clearing it stops the next
		// hello from being diffed against a dead engine's counter.
		_lastSeq = null;
		_setState('closed');
		if (event.code === SLOW_CONSUMER_CODE) {
			console.error(
				`[events-bus] closed as a slow consumer (${event.code} ${event.reason}); refetching`
			);
			_fireResync('slow-consumer');
		}
		if (_shuttingDown) return;
		_scheduleReconnect();
	};
}

function _scheduleReconnect(): void {
	if (_scheduler === null) throw new Error('events-bus: reconnect before connect()');
	if (_retryTimer !== null) return;
	const delay = _retryDelayMs;
	_retryTimer = _scheduler.setTimeout(() => {
		_retryTimer = null;
		if (_shuttingDown) return;
		_open();
	}, delay);
	_retryDelayMs = Math.min(_retryDelayMs * 2, BACKOFF_MAX_MS);
}

/** Close the bus and stop retrying. Subscriptions survive, so a later
 * `connect()` resumes delivering to them. */
export function disconnect(): void {
	_shuttingDown = true;
	if (_retryTimer !== null && _scheduler !== null) {
		_scheduler.clearTimeout(_retryTimer);
		_retryTimer = null;
	}
	const socket = _socket;
	_socket = null;
	if (socket !== null) {
		socket.onopen = null;
		socket.onmessage = null;
		socket.onclose = null;
		socket.onerror = null;
		socket.close();
	}
	_lastSeq = null;
	// The hello describes the engine we were just talking to. Keeping it would
	// let a later connect() diff a fresh engine's contract_rev against a dead
	// one and report a change that is really just a restart, or worse hand
	// getHello() a contract_rev no live engine is serving.
	_hello = null;
	// Backoff belongs to one connection attempt streak. A deliberate close ends
	// that streak, so the next connect() starts at the floor rather than
	// inheriting a 10s delay from whatever went wrong last time.
	_retryDelayMs = BACKOFF_BASE_MS;
	_setState('closed');
}

/** Full teardown for tests: drops every subscription and forgets the hello.
 * Not used by app code, which keeps one bus for the page lifetime. */
export function _resetForTests(): void {
	disconnect();
	_topicListeners.clear();
	_kindListeners.clear();
	_resyncListeners.clear();
	_connectionListeners.clear();
	_hello = null;
	_url = null;
	_socketFactory = null;
	_scheduler = null;
	_retryDelayMs = BACKOFF_BASE_MS;
	_hasConnected = false;
	_shuttingDown = false;
	_state = 'closed';
}
