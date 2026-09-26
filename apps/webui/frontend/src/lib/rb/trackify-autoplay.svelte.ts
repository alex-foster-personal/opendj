/**
 * Trackify unsupervised single-deck autoplay controller (PERFMODE-15).
 */
import { deckAudioClockPositionMs, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import { currentGigRuntimeGeneration } from '$lib/rb/library-mode-runtime';
import { dispatchPerformanceCommand, pushToast } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import {
	pickNextTrackifyCandidate,
	shouldAdvanceTrackify,
	tempoBoundsForTrackify,
	TRACKIFY_DECK_ID,
	TRACKIFY_LOAD_SKIP_DEADLINE_MS,
	type TrackifyDeckSnap
} from '$lib/rb/trackify-autoplay';
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import {
	getTrackifyFeedEpoch,
	getTrackifyFeedRows,
	noteTrackifySkipNext,
	readTrackifySkipNext
} from '$lib/rb/trackify-feed.svelte';

const POLL_MS = 250;

let _timer: ReturnType<typeof setInterval> | null = null;
let _inFlight = false;
let _triggeredFor: string | null = null;
let _playedIds = new Set<string>();
let _quarantinedIds = new Set<string>();
let _playedFeedEpoch = -1;
let _lastSkipReason: string | null = null;
let _queueHead: string | null = null;
/**
 * Bumped by every install/uninstall of the controller. `_loadAndPlay`
 * captures it on entry and re-checks it after every await: a load sequence
 * still unwinding when the route tears down must retire its track (handled
 * by the `_loadGeneration` bump in `installTrackifyAutoplay`'s uninstall)
 * and must not toast or chain into another `_loadAndPlay` retry, which would
 * otherwise start a FRESH, non-superseded generation and could load/play a
 * track on the shared deck slot after this session ended -- e.g. into a Gig
 * session that has since claimed the same deck id (Sol review, PR #3676).
 */
let _installEpoch = 0;

function _deckSnap(): TrackifyDeckSnap {
	const deck = deckStates[TRACKIFY_DECK_ID];
	return {
		stable_id: deck.stable_id,
		playing: deck.playing,
		position_ms: deckAudioClockPositionMs(TRACKIFY_DECK_ID),
		duration_ms: deck.duration_ms
	};
}

function _syncEpoch(): void {
	const epoch = getTrackifyFeedEpoch();
	if (epoch !== _playedFeedEpoch) {
		_playedIds = new Set();
		_quarantinedIds = new Set();
		_triggeredFor = null;
		_playedFeedEpoch = epoch;
	}
}

function _pickNext(feed: readonly AutoPlayTrackRow[], deck: TrackifyDeckSnap): string | null {
	// Default matches the shared helper's own math at pitch=16 (%): a hardcoded
	// stand-in for "no pitch range known", made explicit via the same
	// tempoBoundsFromPitchRange formula the rest of the app uses instead of a
	// second, independently-maintained copy of it.
	const pitch = pitchRanges[TRACKIFY_DECK_ID];
	const bounds = tempoBoundsForTrackify(Number.isFinite(pitch) ? pitch : 16);
	return pickNextTrackifyCandidate({
		feed,
		played_ids: _playedIds,
		quarantined_ids: _quarantinedIds,
		current_id: deck.stable_id,
		current_key: deckStates[TRACKIFY_DECK_ID].key,
		current_bpm: deckStates[TRACKIFY_DECK_ID].bpm,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach
	});
}

/**
 * Races `promise` against `TRACKIFY_LOAD_SKIP_DEADLINE_MS`. A dispatch that
 * never settles (a stalled fetch, a decode that never resolves, a queue that
 * never drains) must still surface as a load failure within the deadline so
 * `_loadAndPlay`'s existing catch block can quarantine it and move on,
 * instead of wedging `_inFlight` permanently (PERFMODE-15 P1).
 */
function _withLoadDeadline<T>(promise: Promise<T>): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const timer = setTimeout(() => {
			reject(
				new Error(`Trackify load did not settle within ${TRACKIFY_LOAD_SKIP_DEADLINE_MS}ms`)
			);
		}, TRACKIFY_LOAD_SKIP_DEADLINE_MS);
		promise.then(
			(value) => {
				clearTimeout(timer);
				resolve(value);
			},
			(error: unknown) => {
				clearTimeout(timer);
				reject(error);
			}
		);
	});
}

/**
 * Generation of the one load sequence allowed to act on the Trackify deck.
 * `_withLoadDeadline` can only reject its own wrapper: the dispatcher offers
 * no cancel for a command that is already running, so a timed-out sequence
 * stays alive underneath. Every step re-checks its generation, and a sequence
 * that was superseded (by its own deadline, or by a newer load) never plays,
 * never becomes queue head, and takes back off the deck a track that its late
 * load published there.
 */
let _loadGeneration = 0;
/**
 * Settles once every dispatch of the most recent load sequence has settled.
 * The dispatcher runs one deck's commands in submission order, so a retry
 * dispatched behind a still-running timed-out load would wait for it inside
 * the dispatcher while its OWN deadline ran, and a healthy candidate would be
 * quarantined for the stale load's latency. The next sequence starts, and its
 * deadline starts, only once the deck is released.
 */
let _lastSequenceSettled: Promise<void> = Promise.resolve();

/**
 * Ceiling on how long `_lastSequenceSettled` may wait for a load sequence's
 * OWN dispatcher call, which has no cancel. `_withLoadDeadline` already
 * bounds a merely SLOW command; this bounds a command that never settles at
 * all. Without it, the raw `sequence` promise never resolves, so every later
 * `_loadAndPlay` call -- including the very retry this one triggers -- awaits
 * `_lastSequenceSettled` forever and `_inFlight` never clears (Sol review,
 * PR #3676: "do not wait forever behind a timed-out load"). Set far above
 * `TRACKIFY_LOAD_SKIP_DEADLINE_MS` so a merely-slow, still-progressing load
 * is never released early into the "nothing may reach the engine past the
 * held deck" window the existing tests already cover.
 */
const _SEQUENCE_HARD_CEILING_MS = 30_000;

function _withHardCeiling(promise: Promise<void>): Promise<void> {
	return new Promise<void>((resolve) => {
		const timer = setTimeout(() => resolve(), _SEQUENCE_HARD_CEILING_MS);
		promise.then(
			() => {
				clearTimeout(timer);
				resolve();
			},
			() => {
				clearTimeout(timer);
				resolve();
			}
		);
	});
}

/**
 * The generation of the most recent `_dispatchLoadSequence` call to have
 * STARTED submitting commands for this deck, set at the top of that
 * function -- before any of its own `await`s. A stale sequence's retirement
 * checks this immediately before its own unload dispatch (Sol review, PR
 * #3676: the dispatcher offers no cancel, so a superseded sequence's own
 * commands can still be sitting mid-queue when it finally settles late).
 * `deckStates[deck].stable_id` is checked at SUBMISSION time, not at the
 * unload's actual EXECUTION time (whenever the scheduler gets to it), so a
 * newer sequence that has ALREADY started dispatching by the time a stale
 * one retires can have its own load command queued behind, then execute,
 * AFTER the stale check passed but BEFORE the stale unload actually runs --
 * the stale unload would then run last and tear the newer, now-current
 * track back off. Once a newer generation has started dispatching, this
 * deck's single slot is that newer sequence's own load to manage (it always
 * replaces whatever was there); a superseded sequence retiring anyway would
 * only ever be racing that newer command for a queue position it cannot win
 * safely.
 */
let _lastDispatchedGeneration = -1;

async function _retireSupersededLoad(
	deck: DeckId,
	nextId: string,
	generation: number,
	ownRuntimeGeneration: number
): Promise<void> {
	if (generation !== _lastDispatchedGeneration) return;
	// A stale retirement must not unload once a DIFFERENT session (Gig, or a
	// remounted Trackify) has since claimed this shared deck id --
	// `currentGigRuntimeGeneration()` only changes when a session actually
	// MOUNTS and claims it (unlike `_installEpoch`, which also bumps on this
	// session's own teardown even when nobody else has claimed the deck yet,
	// a case the existing "late completion only retires the track" test
	// requires to still retire normally). Checking the shared ownership
	// generation instead of `_installEpoch` distinguishes the two (Sol review
	// round 4, PR #3676: "the retirement guard tracks only newer Trackify
	// load generations, not ownership changes to Gig or a remounted
	// session").
	if (currentGigRuntimeGeneration() !== ownRuntimeGeneration) return;
	if (deckStates[deck].stable_id === nextId) {
		await dispatchPerformanceCommand({ type: 'unload', deck });
	}
}

async function _dispatchLoadSequence(
	deck: DeckId,
	nextId: string,
	generation: number,
	ownRuntimeGeneration: number
): Promise<void> {
	_lastDispatchedGeneration = generation;
	const superseded = (): boolean => generation !== _loadGeneration;
	if (deckStates[deck].stable_id !== null && deckStates[deck].stable_id !== nextId) {
		await dispatchPerformanceCommand({ type: 'unload', deck });
		if (superseded()) return _retireSupersededLoad(deck, nextId, generation, ownRuntimeGeneration);
	}
	if (deckStates[deck].stable_id !== nextId) {
		await dispatchPerformanceCommand({
			type: 'load',
			deck,
			stable_id: nextId,
			stems: false,
			suppressCommandErrorToast: true
		});
		if (superseded()) return _retireSupersededLoad(deck, nextId, generation, ownRuntimeGeneration);
	}
	await dispatchPerformanceCommand({ type: 'play', deck, playing: true });
	if (superseded()) return _retireSupersededLoad(deck, nextId, generation, ownRuntimeGeneration);
}

async function _loadAndPlay(nextId: string): Promise<void> {
	const deck = TRACKIFY_DECK_ID;
	const installEpoch = _installEpoch;
	await _lastSequenceSettled;
	// `_lastSequenceSettled` can be a STALE sequence's hard ceiling, still
	// pending up to 30 s after `_loadAndPlay`'s own caller already returned
	// (its 2 s load deadline fired first). Teardown can land during that
	// wait: without this recheck, resuming here would undo teardown's own
	// invalidation and dispatch a fresh load onto a deck a later session
	// (e.g. Gig) may since have claimed (Sol review, PR #3676).
	if (_installEpoch !== installEpoch) return;
	const generation = ++_loadGeneration;
	const sequence = _dispatchLoadSequence(deck, nextId, generation, currentGigRuntimeGeneration());
	// A late failure of a superseded sequence is not re-reported here: its
	// track was already quarantined and toasted when the deadline fired, and
	// the dispatcher has persisted the command error on the deck itself.
	_lastSequenceSettled = _withHardCeiling(
		sequence.then(
			() => undefined,
			() => undefined
		)
	);
	try {
		await _withLoadDeadline(sequence);
		// Torn down while this sequence was in flight: `_dispatchLoadSequence`
		// already retired the track instead of playing it (its own generation
		// check went stale at teardown), so there is nothing left to publish
		// into a dead session (Sol review, PR #3676).
		if (_installEpoch !== installEpoch) return;
		_queueHead = nextId;
	} catch (error: unknown) {
		// Supersede before anything else: from here the sequence may only
		// retire its own track, never play it.
		_loadGeneration += 1;
		// Torn down while this sequence was unwinding: do not toast into a
		// dead session, and do NOT recurse into another _loadAndPlay -- that
		// would mint a fresh, non-superseded generation and could load/play a
		// track on this deck id after a later session (e.g. Gig) has claimed
		// it (Sol review, PR #3676).
		if (_installEpoch !== installEpoch) return;
		_quarantinedIds.add(nextId);
		const message = error instanceof Error ? error.message : String(error);
		_lastSkipReason = `skipped ${nextId}: ${message}`;
		pushToast(`Trackify: skipped track (${message})`, 'info');
		_triggeredFor = null;
		const feed = getTrackifyFeedRows();
		const deckSnap = _deckSnap();
		const retry = _pickNext(feed, deckSnap);
		if (retry !== null && retry !== nextId) {
			await _loadAndPlay(retry);
		}
	}
}

async function _advance(reason: 'end' | 'skip'): Promise<void> {
	const feed = getTrackifyFeedRows();
	const deck = _deckSnap();
	if (deck.stable_id !== null) _playedIds.add(deck.stable_id);
	const nextId = _pickNext(feed, deck);
	if (nextId === null) {
		_lastSkipReason = reason === 'skip' ? 'manual skip exhausted feed' : 'feed exhausted';
		if (feed.length > 0) {
			pushToast('Trackify: no more playable tracks in this feed', 'info');
		}
		// Latch on the track still loaded (unchanged by an exhausted
		// advance), not null: `shouldAdvanceTrackify` treats null as
		// "nothing triggered yet" and would fire again on the very next
		// 250 ms poll, repeating this toast indefinitely until the deck's
		// track or the feed epoch actually changes (both of which already
		// clear this latch elsewhere) (Sol review, PR #3676).
		_triggeredFor = deck.stable_id;
		_queueHead = null;
		return;
	}
	await _loadAndPlay(nextId);
}

async function _tick(): Promise<void> {
	if (!uiPrefs.auto_play_enabled) {
		// Discard rather than leave latched: readTrackifySkipNext() is
		// read-and-clear, and without this a Skip pressed while autoplay is
		// disabled sits on the latch until autoplay is re-enabled, then
		// fires against whatever track happens to be loaded at THAT later
		// moment -- not the one the operator was looking at when they
		// pressed it (Sol review, PR #3676).
		readTrackifySkipNext();
		return;
	}
	_syncEpoch();
	// _inFlight MUST be checked before the skip latch is consumed. The latch
	// (readTrackifySkipNext) is read-and-clear, so if a skip lands while an
	// end-of-track advance from an earlier tick is still awaiting its
	// load/play dispatches, reading it here would start a second, concurrent
	// _advance and clear _inFlight early when that second call finishes
	// (PERFMODE-15 P1). Leaving the latch untouched while in flight re-latches
	// the request for a later tick instead of acting on it now.
	if (_inFlight) return;
	// Captured before any await: teardown resets `_inFlight` to false
	// unconditionally and bumps `_installEpoch`, but does not (and cannot)
	// cancel THIS tick's own still-awaiting `_advance`. If a new session
	// installs right after and sets `_inFlight = true` for its own advance,
	// this tick's `finally` clearing it unconditionally would clear the NEW
	// session's flag out from under it, letting a later tick of the new
	// session start a second, overlapping advance (Sol review, PR #3676).
	// Each `finally` below only clears `_inFlight` while it still owns the
	// epoch it started under.
	const ownEpoch = _installEpoch;
	if (readTrackifySkipNext()) {
		_inFlight = true;
		try {
			await _advance('skip');
		} finally {
			if (_installEpoch === ownEpoch) _inFlight = false;
		}
		return;
	}
	const deck = _deckSnap();
	if (deck.stable_id === null) {
		const feed = getTrackifyFeedRows();
		if (feed.length === 0) return;
		_inFlight = true;
		try {
			const first = _pickNext(feed, deck);
			if (first !== null) await _loadAndPlay(first);
		} finally {
			if (_installEpoch === ownEpoch) _inFlight = false;
		}
		return;
	}
	if (
		shouldAdvanceTrackify({
			enabled: uiPrefs.auto_play_enabled,
			deck,
			already_triggered_for: _triggeredFor,
			in_flight: _inFlight
		})
	) {
		_triggeredFor = deck.stable_id;
		_inFlight = true;
		try {
			await _advance('end');
		} finally {
			if (_installEpoch === ownEpoch) _inFlight = false;
		}
	}
}

export function readTrackifyAutoplayState(): {
	mode: 'trackify';
	autoplay_enabled: boolean;
	queue_head: string | null;
	last_skip_reason: string | null;
	deck: TrackifyDeckSnap;
} {
	return {
		mode: 'trackify',
		autoplay_enabled: uiPrefs.auto_play_enabled,
		queue_head: _queueHead,
		last_skip_reason: _lastSkipReason,
		deck: _deckSnap()
	};
}

export function requestTrackifySkipNext(): void {
	noteTrackifySkipNext();
}

/** Dev/e2e only: drive _loadAndPlay for failure-path acceptance (PERFMODE-15). */
export async function e2eForceTrackifyLoad(stableId: string): Promise<void> {
	if (!import.meta.env.DEV) {
		throw new Error('e2eForceTrackifyLoad is only available in dev builds');
	}
	await _loadAndPlay(stableId);
}

export function installTrackifyAutoplay(): () => void {
	if (_timer !== null) throw new Error('Trackify autoplay is already installed');
	_installEpoch += 1;
	_timer = setInterval(() => {
		void _tick();
	}, POLL_MS);
	return () => {
		if (_timer !== null) clearInterval(_timer);
		_timer = null;
		_inFlight = false;
		_triggeredFor = null;
		_playedIds = new Set();
		_quarantinedIds = new Set();
		_playedFeedEpoch = -1;
		_lastSkipReason = null;
		_queueHead = null;
		// Invalidate every outstanding load sequence from this session. A
		// straggler still mid-dispatch retires its track (via the generation
		// bump) instead of publishing/playing it into whatever session -- or
		// none -- comes next, and `_loadAndPlay`'s own install-epoch check
		// stops it from toasting or retrying into a dead session (Sol review,
		// PR #3676: "invalidate outstanding load sequences during teardown").
		_installEpoch += 1;
		_loadGeneration += 1;
	};
}
