/**
 * The browser half of the Open DJ own-deck observer.
 *
 * PR #605 built the server side: `apps/sets/sources/opendj_source.py` plus
 * `POST /api/sets/deck-observations`. It was fully driveable over HTTP and
 * had tests, but nothing in the browser ever posted to it, so pressing REC
 * recorded zero tracks from Open DJ's own decks. This module and its sibling
 * `deck-observer-install.ts` close that loop and are the only things in the
 * frontend that do. The division: this file is the state machine, deciding
 * what to sample and when to post, and has no DOM dependency at all; the
 * installer owns the mount, the boot window, tab visibility and teardown.
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
 * `deck-snapshot-wire.ts` applies the server's own rules at projection time,
 * so a snapshot that would 422 is counted in `rejected` and never buffered.
 * That matters because `submit_many` validates snapshot by snapshot and
 * raises on the first bad one, AFTER the earlier snapshots in that batch were
 * already enqueued: one malformed deck projection would otherwise reject a
 * whole batch of good observations. A 422 arriving from the server therefore
 * means the two contracts have genuinely diverged, which stops the emitter
 * loudly instead of spamming a doomed retry.
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
import type { DeckId } from '$lib/rb/deck-slots';
import { queryPerformanceState, type PerformanceState } from '$lib/rb/performance-ipc.svelte';
import { externallyRoutedDecks } from './deck-audibility';
import {
	DeckProjectionError,
	toWireSnapshot,
	type DeckObservationWire,
	type DeckSnapshotWire
} from './deck-snapshot-wire';

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

/** Passes `drain` will make before giving up at teardown.
 *  A full buffer is MAX_BUFFERED_SNAPSHOTS / MAX_BATCH_SNAPSHOTS = 3
 *  batches, so three passes empty it and the fourth confirms. The loop's
 *  real bound is PROGRESS - a pass that removes nothing ends it - and this
 *  only backstops a buffer being refilled from elsewhere while draining. */
export const MAX_DRAIN_PASSES = 4;


/** One deck's state on the wire. Field-for-field what
 *  `opendj_wire._parse_deck` accepts. */

/** One instant across every deck. */

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
	/** Why the last discard happened. Deliberately NOT cleared by a later
	 *  success: `last_error` is about what is wrong now, but a discard is a
	 *  permanent hole in a set that a reader still has to be able to explain
	 *  after the next flush works. */
	last_drop_reason: string | null;
}

export interface DeckObserverEmitterOptions {
	/** The engine read. Injected only by tests; production reads the real
	 *  `queryPerformanceState`. */
	readState?: () => PerformanceState;
	/** Wall clock. Injected only by tests. */
	now?: () => Date;
	intervalMs?: number;
	recorderPollMs?: number;
	/** Decks wired straight to a USB output pair. Resolved from the page URL
	 *  in production; injected by tests. */
	routedDecks?: ReadonlySet<DeckId>;
	/** May a batch go out yet? Sampling never asks: it is a local read and
	 *  costs the boot window nothing. Posting does, so the installer holds it
	 *  shut until the boot burst clears. Default open. */
	canFlush?: () => boolean;
}

/** The answer to "who is recording", including "the question did not get an
 *  answer", which a caller must not confuse with "nobody". */
export type RecorderCheck = DeckObserverStatus['phase'] | 'unknown';

export interface DeckObserverEmitter {
	start: () => void;
	stop: () => void;
	status: () => DeckObserverStatus;
	/** One cadence tick: sample, then flush. Exposed so a test (and an
	 *  agent) can drive the loop without waiting on a timer. */
	tick: () => Promise<void>;
	sampleOnce: () => void;
	flushOnce: () => Promise<void>;
	/** Empty the buffer, waiting out a POST already in flight. What teardown
	 *  must call: `flushOnce` returns without sending while one is in flight,
	 *  which is correct for a tick and loses the buffer for a last try. */
	drain: () => Promise<void>;
	/** Ask whether a recording is running. Returns the resulting phase, or
	 *  `'unknown'` if the request itself failed -- which is not `'idle'` and
	 *  not the phase the caller was already in. */
	refreshRecorder: () => Promise<RecorderCheck>;
}

/** A held snapshot plus the recording it was sampled under.
 *
 *  Tagged per snapshot rather than per buffer because a flush awaits a network
 *  round trip, and a `pagehide` flush can be in flight while a tick discovers a
 *  new recorder and samples under it. A single buffer-wide stamp then labels
 *  those fresh samples with the session that just ended. */
interface BufferedSnapshot {
	snapshot: DeckSnapshotWire;
	sessionId: string | null;
	/** `observed_at` in ms. Kept beside the snapshot so the clock guard can be
	 *  re-derived from what is still held, rather than from a remembered
	 *  number that outlives the session it belonged to. */
	atMs: number;
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
	const routedDecks =
		options.routedDecks ??
		externallyRoutedDecks(typeof window === 'undefined' ? '' : window.location.search);
	const canFlush = options.canFlush ?? ((): boolean => true);

	let phase: DeckObserverStatus['phase'] = 'idle';
	let sessionId: string | null = null;
	/** Set whenever a flush left snapshots buffered. The recorder must be
	 *  re-read before those snapshots are offered again -- see `tick`. This
	 *  narrows the window; the server-side `session_id` check is what closes
	 *  it, because this one is stale the moment the await returns. */
	let reverifyBeforeFlush = false;
	let sampled = 0;
	let posted = 0;
	let rejected = 0;
	let dropped = 0;
	let clockRegressions = 0;
	let lastError: string | null = null;
	let lastDropReason: string | null = null;
	let buffer: BufferedSnapshot[] = [];
	/** The floor the next sample must not fall below.
	 *
	 *  SCOPED TO THE SESSION, because the server's is: `record.py` builds a
	 *  fresh `OpenDjDeckSource` per recording, so `_last_submitted_at` starts
	 *  null for each one. Carrying this across a session boundary is therefore
	 *  STRICTER than the server, and a wall clock that regresses between two
	 *  recordings would make the client refuse an entire set the server would
	 *  have accepted. Within a session it must persist, because there the
	 *  server's floor persists across requests too. */
	let lastObservedAtMs: number | null = null;
	let lastRecorderPollMs: number | null = null;
	/** The POST currently in flight, or null. A PROMISE rather than a
	 *  boolean, because teardown needs to WAIT for it and a boolean can
	 *  only be observed. See `drain`. */
	let inFlight: Promise<void> | null = null;
	let tickInFlight = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	/** Set by `stop()`, checked after every await inside a tick.
	 *
	 *  `stop()` clears the interval, which is enough for ticks that have not
	 *  started and nothing at all for the one that has. `start()` fires an
	 *  immediate `void tick()`, and that tick's recorder GET can still be in
	 *  flight when /performance unmounts; its continuation then samples a deck
	 *  engine that is being disposed - or, after a remount, somebody else's -
	 *  and POSTs off-route. Two emitters posting out of timestamp order earn a
	 *  422, and the live one stops observing the rest of the set. Codex found
	 *  it on #709.
	 *
	 *  Deliberately NOT consulted by `flushOnce`: teardown flushes the buffer
	 *  and only then calls `stop()`, so gating the flush here would delete the
	 *  boot-window save this same teardown exists to make. */
	let stopped = false;

	const status = (): DeckObserverStatus => ({
		phase,
		session_id: sessionId,
		sampled,
		posted,
		buffered: buffer.length,
		rejected,
		dropped,
		clock_regressions: clockRegressions,
		last_error: lastError,
		last_drop_reason: lastDropReason
	});

	/** Re-derive the clock floor from what is STILL HELD. Called only where
	 *  the emitter stops believing in the session it was sampling: an empty
	 *  buffer means nothing constrains the next sample, and a non-empty one
	 *  means its newest entry does. */
	const rebindClockToBuffer = (): void => {
		lastObservedAtMs = buffer.length === 0 ? null : buffer[buffer.length - 1].atMs;
	};

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
			snapshot = toWireSnapshot(readState(), at, routedDecks);
		} catch (error) {
			rejected += 1;
			lastError = _errorText(error);
			console.error('[deck-observer] refusing to send an impossible deck state:', error);
			return;
		}
		lastObservedAtMs = atMs;
		buffer.push({ snapshot, sessionId, atMs });
		sampled += 1;
		if (buffer.length > MAX_BUFFERED_SNAPSHOTS) {
			const overflow = buffer.length - MAX_BUFFERED_SNAPSHOTS;
			buffer = buffer.slice(overflow);
			dropped += overflow;
			lastDropReason = `buffer overflow: dropped ${overflow} oldest snapshot(s)`;
			lastError = lastDropReason;
		}
	};

	const flushOnce = async (): Promise<void> => {
		if (inFlight !== null || buffer.length === 0) return;
		// Held shut during the boot window. Snapshots keep accumulating, so
		// nothing is lost; only the POST waits.
		if (!canFlush()) return;
		// The LEADING RUN sampled under one session, never a mixed batch. The
		// session travels with each snapshot rather than with the buffer as a
		// whole because the buffer outlives a recording: a stop-then-start
		// while snapshots are held leaves old and new samples in the same
		// array, and one stamp for the array would mislabel one of them.
		const sampledUnder = buffer[0].sessionId;
		// UNBOUND SNAPSHOTS GO NOWHERE, and that is now said here rather than
		// discovered by the server. `session_id` became REQUIRED on the wire
		// this round, so a batch with none earns a 422; before that it was
		// accepted and filed under whichever set happened to be recording.
		// `tick` cannot produce these (it samples only in the emitting phase),
		// so reaching this means the exposed `sampleOnce` was driven off a
		// recording - by an agent, or by a route that mounted with REC off.
		// Dropping and counting them is the same verdict the server reaches,
		// one hop earlier and with a reason attached; holding them would park
		// an unsendable entry at the head of the buffer forever.
		if (sampledUnder === null) {
			const orphans = buffer.filter((held) => held.sessionId === null).length;
			lastDropReason = `no recording was bound: dropped ${orphans} unbound snapshot(s)`;
			dropped += orphans;
			buffer = buffer.filter((held) => held.sessionId !== null);
			rebindClockToBuffer();
			lastError = lastDropReason;
			return;
		}
		// Published before the first await so a concurrent caller sees it, and
		// resolved in the `finally` below. `settle` is the resolver; nothing
		// outside this function ever calls it.
		let settle = (): void => {};
		inFlight = new Promise<void>((resolve) => {
			settle = resolve;
		});
		const sent: BufferedSnapshot[] = [];
		for (const held of buffer) {
			if (held.sessionId !== sampledUnder || sent.length >= MAX_BATCH_SNAPSHOTS) break;
			sent.push(held);
		}
		const batch = sent.map((held) => held.snapshot);
		// Removing what was SENT, by identity, rather than slicing a count off
		// the front. The buffer is mutable and the POST is a round trip, so by
		// the time it answers these entries may no longer be at index 0 -- a
		// tick can have dropped a stale run or appended fresh samples while
		// this was in flight, and a positional slice would then delete
		// somebody else's snapshots.
		const dropSent = (): void => {
			const sentSet = new Set(sent);
			buffer = buffer.filter((held) => !sentSet.has(held));
		};
		try {
			await api.POST('/api/sets/deck-observations', {
				body: {
					snapshots: batch as unknown as Record<string, unknown>[],
					session_id: sampledUnder
				}
			});
			dropSent();
			posted += batch.length;
			lastError = null;
		} catch (error) {
			if (error instanceof ApiError && error.status === 409) {
				// Nothing is recording, or the live session did not enable the
				// opendj_decks source. These observations have nowhere to go, so
				// they are discarded and counted rather than retried forever.
				lastDropReason = `no live recorder: dropped ${batch.length} snapshot(s)`;
				dropped += batch.length;
				dropSent();
				// A stale answer removes its OWN batch and nothing else. The
				// batch identifies itself by the session it was sampled under:
				// if that is still what we believe, this 409 is news and the
				// emitter goes idle. If a tick has since discovered a different
				// recorder, this response predates that discovery and must not
				// overwrite it -- doing so would also suppress sampling until
				// the poll interval expires, because that tick already bumped
				// lastRecorderPollMs, so a live set would be undercounted while
				// the emitter waited to re-learn what it had just been told.
				if (sessionId === sampledUnder) {
					phase = 'idle';
					sessionId = null;
					// This session is over, so its clock floor goes with it.
					rebindClockToBuffer();
				}
				lastError = _errorText(error);
			} else if (error instanceof ApiError && error.status === 422) {
				// Local validation already applied every rule the server applies,
				// so a 422 means the two contracts have diverged. Retrying would
				// spam an endpoint that will refuse this batch every time.
				lastDropReason = `wire contract diverged: dropped ${batch.length} snapshot(s)`;
				dropped += batch.length;
				dropSent();
				phase = 'stopped';
				lastError = _errorText(error);
				console.error('[deck-observer] wire contract diverged; emitter stopped:', error);
			} else {
				// A transport failure. Keep the batch and try again next tick --
				// but not blindly: the recording that these snapshots belong to
				// may be stopped and replaced before the retry lands, so the
				// next tick re-reads the recorder before offering them.
				reverifyBeforeFlush = true;
				lastError = _errorText(error);
			}
		} finally {
			inFlight = null;
			settle();
		}
	};

	/**
	 * Empty the buffer, waiting out any POST already in flight.
	 *
	 * `flushOnce` returns immediately when one is in flight, which is right
	 * for a tick - the next tick will try again - and WRONG for teardown,
	 * because teardown is the last try there will be. The sequence Codex
	 * found on #709: a deferred boot flush or a `pagehide` POST is still in
	 * flight when the next tick appends a snapshot; /performance then
	 * unmounts, the teardown flush returns without sending because the flag
	 * is set, `stop()` cancels the only future retry, and the original POST
	 * removes only its OWN captured entries. The later snapshot stays
	 * buffered forever and the set is under-counted by exactly the samples
	 * taken during that window - silently, since nothing is dropped or
	 * errored.
	 *
	 * Bounded by PROGRESS, not by a timeout: each pass must remove at least
	 * one snapshot or the loop ends. A server that is refusing, or a boot
	 * window that has not opened, therefore costs one wasted pass rather
	 * than spinning - and those snapshots are genuinely unsendable, so
	 * holding them is the honest outcome. MAX_DRAIN_PASSES is the backstop
	 * for a buffer that keeps growing from another source while draining.
	 */
	const drain = async (): Promise<void> => {
		for (let pass = 0; pass < MAX_DRAIN_PASSES; pass += 1) {
			if (inFlight !== null) await inFlight;
			if (buffer.length === 0) return;
			const before = buffer.length;
			await flushOnce();
			if (buffer.length >= before) return;
		}
	};

	/**
	 * Re-read who is recording.
	 *
	 * Returns `'unknown'` when the GET itself failed, which is NOT the same as
	 * `'idle'` and not the same as the phase we happened to be in. The catch
	 * used to swallow the failure and hand back the STALE phase, so a caller
	 * asking "is the recorder still the one I sampled under" got back its own
	 * prior belief and read it as confirmation.
	 */
	const refreshRecorder = async (): Promise<RecorderCheck> => {
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
			return 'unknown';
		}
		return phase;
	};

	const tickBody = async (): Promise<void> => {
		if (phase === 'stopped') return;
		if (phase === 'idle') {
			// A NEGATIVE elapsed means the wall clock moved backwards since the
			// last poll, so that stamp describes a future this run never had and
			// tells us nothing about how stale the recorder answer is. Treat it
			// as "never polled" rather than waiting for the clock to catch up:
			// the alternative is an emitter that sits idle through a whole set
			// because an NTP correction landed between two recordings. Found
			// while testing the same defect one layer down, in the observation
			// clock floor.
			const elapsed =
				lastRecorderPollMs === null ? Infinity : now().getTime() - lastRecorderPollMs;
			const sinceLastPoll = elapsed < 0 ? Infinity : elapsed;
			// Read the phase back off refreshRecorder rather than the closed-over
			// variable: TypeScript narrowed it to 'idle' at the branch above and
			// cannot see that the await reassigned it.
			const resolved = sinceLastPoll >= recorderPollMs ? await refreshRecorder() : phase;
			if (stopped) return;
			if (resolved !== 'emitting') return;
		} else if (reverifyBeforeFlush) {
			// A previous flush failed in transport, so snapshots from the session
			// that was live then are still buffered. Nothing in the request names
			// a session, so if that recording was stopped and another started in
			// the gap, posting the backlog now would file the previous set's
			// playback under the new one. Re-read the recorder and bind to it.
			// The session the BATCH ABOUT TO BE OFFERED was sampled under, read
			// off the buffer itself rather than off `sessionId`. Those two
			// agreed while one stamp covered the whole buffer; now that each
			// snapshot carries its own, `sessionId` is the CURRENT belief and
			// using it here would drop the run that is still valid.
			const boundTo = buffer.length > 0 ? buffer[0].sessionId : sessionId;
			reverifyBeforeFlush = false;
			const resolved = await refreshRecorder();
			if (stopped) return;
			if (resolved === 'unknown') {
				// The check did not answer, so nothing was confirmed. Re-arm it,
				// keep sampling, and hold the backlog until a real answer comes.
				reverifyBeforeFlush = true;
				sampleOnce();
				return;
			}
			if (resolved !== 'emitting' || sessionId !== boundTo) {
				// Only the snapshots belonging to the session that went away.
				// Anything sampled since is either already tagged with the new
				// session or will be, and dropping it here would delete
				// observations of the recording that is running right now.
				const stale = buffer.filter((held) => held.sessionId === boundTo).length;
				// Only when something was actually discarded. This branch also
				// runs for a recorder that merely stopped with an empty buffer,
				// and `last_drop_reason` exists to explain a permanent hole in a
				// set -- writing "dropped 0 snapshot(s)" into it would make the
				// one field that is meant to be honest report a loss that never
				// happened.
				if (stale > 0) {
					lastDropReason =
						`recorder session changed from ${boundTo ?? 'none'} to ` +
						`${sessionId ?? 'none'} while ${stale} snapshot(s) were ` +
						'buffered; dropped rather than misattributed';
					dropped += stale;
					buffer = buffer.filter((held) => held.sessionId !== boundTo);
					rebindClockToBuffer();
					lastError = lastDropReason;
				}
				if (resolved !== 'emitting') return;
			}
		}
		// Last gate before anything with an effect. Everything above this line
		// either reads or decides; `sampleOnce` touches the deck engine and
		// `flushOnce` POSTs, and both are wrong to do once the emitter is gone.
		if (stopped) return;
		sampleOnce();
		await flushOnce();
	};

	/**
	 * One tick at a time.
	 *
	 * `setInterval` fires without awaiting the previous promise, so a recorder
	 * GET that outlives one interval used to let the NEXT tick run with
	 * `reverifyBeforeFlush` already cleared and post the backlog while the
	 * check that was meant to gate it was still in flight. Dropping the
	 * overlapping tick is the right answer rather than queueing it: the work it
	 * would do is take one snapshot, and the server credits at most
	 * MAX_SNAPSHOT_GAP_S of dwell across a gap anyway.
	 */
	const tick = async (): Promise<void> => {
		if (tickInFlight) return;
		tickInFlight = true;
		try {
			await tickBody();
		} finally {
			tickInFlight = false;
		}
	};

	return {
		start: (): void => {
			if (timer !== null) return;
			stopped = false;
			// Tick NOW, not one interval from now. The first snapshot banks no
			// dwell server-side (`_credit` needs a previous observation of that
			// deck), so on a set already running the clock does not really start
			// until the SECOND sample. Waiting an interval for the first one
			// therefore costs two intervals of a live set, not one.
			void tick();
			timer = setInterval(() => void tick(), intervalMs);
		},
		stop: (): void => {
			// Set BEFORE the early return: `start()` fires an immediate tick and
			// only then assigns `timer`, so a stop that lands inside that first
			// tick's await window sees `timer === null` and would otherwise
			// leave the tick running with nothing to cancel it.
			stopped = true;
			if (timer === null) return;
			clearInterval(timer);
			timer = null;
		},
		status,
		tick,
		sampleOnce,
		flushOnce,
		drain,
		refreshRecorder
	};
}
