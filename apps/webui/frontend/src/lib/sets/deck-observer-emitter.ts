/**
 * The browser half of the Open DJ own-deck observer.
 *
 * PR #605 built the server side: `apps/sets/sources/opendj_source.py` plus
 * `POST /api/sets/deck-observations`. It was fully driveable over HTTP and
 * had tests, but nothing in the browser ever posted to it, so pressing REC
 * recorded zero tracks from Open DJ's own decks. This module closes that
 * loop and is the only thing in the frontend that does.
 *
 * WHY A SNAPSHOT STREAM RATHER THAN TRANSITION EVENTS
 * The wire unit is a periodic snapshot of every deck, chosen server-side so
 * that a browser which reloads, crashes or is closed mid-track still leaves
 * every second it already reported banked in sqlite. This module therefore
 * samples on a timer and never tries to detect "a track started"; deciding
 * what was played is the server's job, and filtering what counts as played
 * is a read-time question (docs/product/set-dwell-threshold-analysis.md).
 *
 * WHY IT ONLY READS performance-ipc
 * `queryPerformanceState()` is the one existing source of truth for deck
 * state, and three branches were open across `performance-ipc.svelte.ts`
 * and `audio-engine.svelte.ts` when this was written. So this module reads
 * that function and changes nothing in it: a new file cannot collide with a
 * lane it never edits.
 *
 * WHY IT VALIDATES BEFORE BUFFERING
 * `submit_many` on the server validates snapshot by snapshot and raises on
 * the first bad one, which 422s the request AFTER the earlier snapshots in
 * that batch were already enqueued. One malformed deck projection would
 * therefore reject a whole batch of good observations. So the same rules
 * `apps/sets/sources/opendj_wire.py:parse_snapshot` applies are applied
 * here, before anything is buffered, and a snapshot that fails them is
 * counted in `rejected` rather than being posted or silently dropped. A
 * 422 from the server then means the two contracts have genuinely diverged,
 * which stops the emitter loudly instead of spamming a doomed retry.
 *
 * BACKGROUNDED TABS
 * Chrome exempts pages that are playing audio from intensive timer
 * throttling, so a live deck keeps this timer near its 1 s cadence even
 * when hidden. That is not guaranteed, so samples are buffered and flushed
 * in batches: whatever the tab did see is delivered, and the server credits
 * at most MAX_SNAPSHOT_GAP_S (5 s) of dwell across any single gap, so a
 * throttled tab under-counts rather than inventing playback.
 */

import { ApiError, api } from '$lib/api/client';
import { queryPerformanceState, type PerformanceState } from '$lib/rb/performance-ipc.svelte';

/** Target cadence. Mirrors SNAPSHOT_INTERVAL_S in
 *  apps/sets/sources/opendj_source.py, which is the published number both
 *  sides agree on. */
export const SNAPSHOT_INTERVAL_MS = 1000;

/** How much dwell the server will credit across one gap between snapshots
 *  (MAX_SNAPSHOT_GAP_S). Not enforced here; it is the budget this module's
 *  cadence has to stay inside, and it is why flushing is batched. */
export const MAX_SNAPSHOT_GAP_MS = 5000;

/** DeckObservationsRequest.snapshots is capped at 600 server-side. */
export const MAX_BATCH_SNAPSHOTS = 600;

/** Thirty minutes of buffered 1 s samples. A buffer that grew without a
 *  bound would trade a lost set for an out-of-memory tab; overflow drops the
 *  OLDEST samples and is counted in `dropped`, never silent. */
export const MAX_BUFFERED_SNAPSHOTS = 1800;

/** How often to ask whether a recording has started, while idle. */
export const RECORDER_POLL_MS = 5000;

/** One deck's state on the wire. Field-for-field what
 *  `opendj_wire._parse_deck` accepts. */
export interface DeckObservationWire {
	stable_id: string | null;
	playing: boolean;
	audible: boolean;
	position_ms: number;
	duration_ms: number | null;
	title: string | null;
	artist: string | null;
}

/** One instant across every deck. */
export interface DeckSnapshotWire {
	/** UTC ISO 8601 with a real offset, from `Date.prototype.toISOString`.
	 *  The zone is produced by the clock and never typed by hand. */
	observed_at: string;
	decks: Record<string, DeckObservationWire>;
}

export interface DeckObserverStatus {
	/** `idle` while no recording is running, `emitting` while posting,
	 *  `stopped` once a contract divergence took the emitter down. */
	phase: 'idle' | 'emitting' | 'stopped';
	session_id: string | null;
	/** Snapshots taken from the engine and accepted by local validation. */
	sampled: number;
	/** Snapshots the server has accepted. */
	posted: number;
	/** Snapshots waiting to be posted. */
	buffered: number;
	/** Snapshots local validation refused, i.e. engine states the server
	 *  contract calls impossible. */
	rejected: number;
	/** Snapshots discarded: buffer overflow, or a recorder that stopped
	 *  before they could be posted. */
	dropped: number;
	/** Samples skipped because the wall clock moved backwards, which would
	 *  break the non-decreasing order the server requires. */
	clock_regressions: number;
	last_error: string | null;
}

export interface DeckObserverEmitterOptions {
	/** The engine read. Injected only by tests; production reads the real
	 *  `queryPerformanceState`. */
	readState?: () => PerformanceState;
	/** Wall clock. Injected only by tests. */
	now?: () => Date;
	intervalMs?: number;
	recorderPollMs?: number;
}

export interface DeckObserverEmitter {
	start: () => void;
	stop: () => void;
	status: () => DeckObserverStatus;
	/** One cadence tick: sample, then flush. Exposed so a test (and an
	 *  agent) can drive the loop without waiting on a timer. */
	tick: () => Promise<void>;
	sampleOnce: () => void;
	flushOnce: () => Promise<void>;
	/** Ask whether a recording is running. Returns the resulting phase. */
	refreshRecorder: () => Promise<DeckObserverStatus['phase']>;
}

/** A deck state the server contract says cannot exist. Never coerced. */
export class DeckProjectionError extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'DeckProjectionError';
	}
}

// ------------------------------------------------------------- projection ---

const WIRE_DECK_IDS = [1, 2, 3, 4] as const;

function _requireBool(value: unknown, field: string, deck: number): boolean {
	if (typeof value !== 'boolean') {
		throw new DeckProjectionError(`deck ${deck}: ${field} must be a bool, got ${String(value)}`);
	}
	return value;
}

function _requirePositionMs(value: unknown, deck: number): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new DeckProjectionError(
			`deck ${deck}: position_ms must be a finite number, got ${String(value)}`
		);
	}
	if (value < 0) {
		throw new DeckProjectionError(`deck ${deck}: position_ms must not be negative, got ${value}`);
	}
	return value;
}

/** `duration_ms` is null for a deck with nothing loaded, and the server
 *  rejects a non-positive duration outright. A zero-length deck is the
 *  "length not known yet" state, so it travels as null rather than as a
 *  number the far side would refuse. */
function _requireDurationMs(value: unknown, deck: number): number | null {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new DeckProjectionError(
			`deck ${deck}: duration_ms must be a finite number or null, got ${String(value)}`
		);
	}
	return value > 0 ? value : null;
}

function _optionalString(value: unknown, field: string, deck: number): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'string') {
		throw new DeckProjectionError(`deck ${deck}: ${field} must be a string or null`);
	}
	return value;
}

function _requireStableId(value: unknown, deck: number): string | null {
	if (value === null || value === undefined) return null;
	if (typeof value !== 'string') {
		throw new DeckProjectionError(`deck ${deck}: stable_id must be a string or null`);
	}
	if (value === '') {
		throw new DeckProjectionError(
			`deck ${deck}: stable_id must not be empty; an empty deck sends null`
		);
	}
	return value;
}

/**
 * Project one engine state onto the wire contract, or throw.
 *
 * Exported because this is the half worth testing exhaustively: everything
 * the server refuses has to be refused here first, or one bad deck loses a
 * whole batch of good observations.
 */
export function toWireSnapshot(state: PerformanceState, observedAt: Date): DeckSnapshotWire {
	const decks: Record<string, DeckObservationWire> = {};
	for (const deckId of WIRE_DECK_IDS) {
		const deck = state.decks[deckId];
		if (deck === undefined) {
			throw new DeckProjectionError(`deck ${deckId}: missing from the engine state`);
		}
		const stableId = _requireStableId(deck.stable_id, deckId);
		const audible = _requireBool(deck.audible, 'audible', deckId);
		if (audible && stableId === null) {
			throw new DeckProjectionError(
				`deck ${deckId}: audible with no track loaded is not a state a deck can be in`
			);
		}
		decks[String(deckId)] = {
			stable_id: stableId,
			playing: _requireBool(deck.playing, 'playing', deckId),
			audible,
			position_ms: _requirePositionMs(deck.position_ms, deckId),
			duration_ms: _requireDurationMs(deck.duration_ms, deckId),
			title: _optionalString(deck.title, 'title', deckId),
			artist: _optionalString(deck.artist, 'artist', deckId)
		};
	}
	return { observed_at: observedAt.toISOString(), decks };
}

// ----------------------------------------------------------------- emitter ---

function _errorText(error: unknown): string {
	if (error instanceof ApiError) return `${error.status} ${error.message}`;
	return error instanceof Error ? error.message : String(error);
}

export function createDeckObserverEmitter(
	options: DeckObserverEmitterOptions = {}
): DeckObserverEmitter {
	const readState = options.readState ?? queryPerformanceState;
	const now = options.now ?? (() => new Date());
	const intervalMs = options.intervalMs ?? SNAPSHOT_INTERVAL_MS;
	const recorderPollMs = options.recorderPollMs ?? RECORDER_POLL_MS;

	let phase: DeckObserverStatus['phase'] = 'idle';
	let sessionId: string | null = null;
	let sampled = 0;
	let posted = 0;
	let rejected = 0;
	let dropped = 0;
	let clockRegressions = 0;
	let lastError: string | null = null;
	let buffer: DeckSnapshotWire[] = [];
	let lastObservedAtMs: number | null = null;
	let lastRecorderPollMs: number | null = null;
	let flushInFlight = false;
	let timer: ReturnType<typeof setInterval> | null = null;

	const status = (): DeckObserverStatus => ({
		phase,
		session_id: sessionId,
		sampled,
		posted,
		buffered: buffer.length,
		rejected,
		dropped,
		clock_regressions: clockRegressions,
		last_error: lastError
	});

	const sampleOnce = (): void => {
		const at = now();
		const atMs = at.getTime();
		if (lastObservedAtMs !== null && atMs < lastObservedAtMs) {
			// The server requires non-decreasing observed_at and would 422 the
			// batch. Skipping is a missed tick, which the dwell accumulator
			// already handles; rewriting the stamp would be a fabrication.
			clockRegressions += 1;
			lastError = 'wall clock moved backwards; snapshot skipped';
			return;
		}
		let snapshot: DeckSnapshotWire;
		try {
			snapshot = toWireSnapshot(readState(), at);
		} catch (error) {
			rejected += 1;
			lastError = _errorText(error);
			console.error('[deck-observer] refusing to send an impossible deck state:', error);
			return;
		}
		lastObservedAtMs = atMs;
		buffer.push(snapshot);
		sampled += 1;
		if (buffer.length > MAX_BUFFERED_SNAPSHOTS) {
			const overflow = buffer.length - MAX_BUFFERED_SNAPSHOTS;
			buffer = buffer.slice(overflow);
			dropped += overflow;
			lastError = `buffer overflow: dropped ${overflow} oldest snapshot(s)`;
		}
	};

	const flushOnce = async (): Promise<void> => {
		if (flushInFlight || buffer.length === 0) return;
		flushInFlight = true;
		const batch = buffer.slice(0, MAX_BATCH_SNAPSHOTS);
		try {
			await api.POST('/api/sets/deck-observations', {
				body: { snapshots: batch as unknown as Record<string, unknown>[] }
			});
			buffer = buffer.slice(batch.length);
			posted += batch.length;
			lastError = null;
		} catch (error) {
			if (error instanceof ApiError && error.status === 409) {
				// Nothing is recording, or the live session did not enable the
				// opendj_decks source. These observations have nowhere to go, so
				// they are discarded and counted rather than retried forever.
				dropped += buffer.length;
				buffer = [];
				phase = 'idle';
				sessionId = null;
				lastObservedAtMs = null;
				lastError = _errorText(error);
			} else if (error instanceof ApiError && error.status === 422) {
				// Local validation already applied every rule the server applies,
				// so a 422 means the two contracts have diverged. Retrying would
				// spam an endpoint that will refuse this batch every time.
				dropped += buffer.length;
				buffer = [];
				phase = 'stopped';
				lastError = _errorText(error);
				console.error('[deck-observer] wire contract diverged; emitter stopped:', error);
			} else {
				// A transport failure. Keep the batch and try again next tick.
				lastError = _errorText(error);
			}
		} finally {
			flushInFlight = false;
		}
	};

	const refreshRecorder = async (): Promise<DeckObserverStatus['phase']> => {
		lastRecorderPollMs = now().getTime();
		try {
			const { data } = await api.GET('/api/sets/recorder', {});
			if (data?.active === true) {
				phase = 'emitting';
				sessionId = data.session_id ?? null;
			} else {
				phase = 'idle';
				sessionId = null;
			}
		} catch (error) {
			lastError = _errorText(error);
		}
		return phase;
	};

	const tick = async (): Promise<void> => {
		if (phase === 'stopped') return;
		if (phase === 'idle') {
			const sinceLastPoll =
				lastRecorderPollMs === null ? Infinity : now().getTime() - lastRecorderPollMs;
			// Read the phase back off refreshRecorder rather than the closed-over
			// variable: TypeScript narrowed it to 'idle' at the branch above and
			// cannot see that the await reassigned it.
			const resolved = sinceLastPoll >= recorderPollMs ? await refreshRecorder() : phase;
			if (resolved !== 'emitting') return;
		}
		sampleOnce();
		await flushOnce();
	};

	return {
		start: (): void => {
			if (timer !== null) return;
			timer = setInterval(() => void tick(), intervalMs);
		},
		stop: (): void => {
			if (timer === null) return;
			clearInterval(timer);
			timer = null;
		},
		status,
		tick,
		sampleOnce,
		flushOnce,
		refreshRecorder
	};
}

/** The window global name an agent drives this through, so the emitter is
 *  inspectable and flushable without a UI (agent-native parity). */
export const DECK_OBSERVER_GLOBAL = '__mdtDeckObserver';

/**
 * Start observing for the lifetime of /performance. Returns the uninstall,
 * which the caller owns, matching the other `install*` hooks that route
 * mounts.
 *
 * It lives on /performance rather than in the root layout because the deck
 * engine only exists there: off-route the audio graph is disposed, so there
 * is no deck state to observe, and a root-layout import would also drag the
 * route's DSP into every other page's bundle.
 */
export function installDeckObserverEmitter(
	options: DeckObserverEmitterOptions = {}
): () => void {
	if (typeof window === 'undefined' || typeof document === 'undefined') {
		return () => {};
	}
	const emitter = createDeckObserverEmitter(options);
	emitter.start();

	// A tab going away is the case the buffer exists for: flush what it saw
	// before the page is frozen or discarded.
	const flushOnHide = (): void => {
		if (document.visibilityState === 'hidden') void emitter.flushOnce();
	};
	document.addEventListener('visibilitychange', flushOnHide);
	window.addEventListener('pagehide', flushOnHide);

	Object.defineProperty(window, DECK_OBSERVER_GLOBAL, {
		value: {
			status: emitter.status,
			tick: emitter.tick,
			flush: emitter.flushOnce
		},
		configurable: true,
		writable: true,
		enumerable: false
	});

	return () => {
		emitter.stop();
		document.removeEventListener('visibilitychange', flushOnHide);
		window.removeEventListener('pagehide', flushOnHide);
		Reflect.deleteProperty(window, DECK_OBSERVER_GLOBAL);
	};
}
