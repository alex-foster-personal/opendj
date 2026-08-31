/**
 * /performance auto-play controller (v1 hard-cut).
 *
 * When prefs.auto_play_enabled: as the MASTER deck (never a non-master playing
 * deck - see pickSourceDeck) enters the remaining-time window, load the next
 * playlist track onto a free or stopped follower and start it via
 * performance-ipc. Default pick: earliest un-played membership row with
 * Camelot key +-1 and BPM inside Beat Sync pitch bounds (optional
 * maximize-reach / Warnsdorff slack). Optional enforce_play_order walks strict
 * playlist order after current.
 *
 * The full scenario matrix this controller implements lives at the top of
 * auto-play.ts. Read it before changing anything here.
 *
 * Beat Sync is requested only when a real BAR/BEAT phase-lock plan succeeds;
 * otherwise follower Beat Sync is explicitly disabled and play continues
 * free-tempo (see .planning/beat-sync-phase-lock-explainer-SA.md).
 *
 * Failed handoffs quarantine the candidate and re-arm (bounded) so ghost
 * tracks / mapping 404s cannot permanently disarm AutoPlay for the source -
 * but ONLY while nothing has landed on the follower deck (matrix rows 16/17).
 */
import { DECK_IDS, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import {
	AUTO_PLAY_THRESHOLD_MS,
	autoPlayExcludedIds,
	decideAutoPlayBeatSync,
	decideMasterPromotion,
	handoffFailureIsRetryable,
	formatAutoPlaySyncSkipToast,
	getAutoPlayFeedEpoch,
	getAutoPlayPlaylist,
	pickFollowerDeck,
	pickNextStableId,
	pickSourceDeck,
	remainingMs,
	shouldTriggerAutoPlay,
	simulateAutoPlayChain,
	tempoBoundsFromPitchRange,
	type AutoPlayDeckSnap,
	type AutoPlayHandoffPhase
} from '$lib/rb/auto-play';
import {
	computeFollowerSyncPlan,
	quantizeToNearestBeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';
import type { AnlzBeat } from '$lib/rb/types';
import type { DeckId } from '$lib/rb/types';

const POLL_MS = 250;
/** Synthetic schedule horizon for preflight only (plan needs syncAt > now). */
const PREFLIGHT_SYNC_AHEAD_SEC = 0.05;
/** Failed load/play attempts per source track before stopping. */
const MAX_HANDOFF_ATTEMPTS = 3;

let _timer: ReturnType<typeof setInterval> | null = null;
let _inFlight = false;
let _triggeredFor: string | null = null;
let _playedIds = new Set<string>();
let _playedFeedEpoch = -1;
/** Candidates that failed load/play; cleared when playlist membership changes. */
let _unplayableIds = new Set<string>();
let _attemptsFor: { source: string; count: number } = { source: '', count: 0 };
/** Toast-once while waiting for a free follower (does not pin _triggeredFor). */
let _waitingFollowerFor: string | null = null;
/**
 * Every stable_id AutoPlay has DECIDED to load this session (matrix row 14).
 * Recorded before dispatch, never rolled back, and deliberately NOT cleared by
 * _syncPlayedSet: this is the guarantee that AutoPlay cannot put one track on
 * two decks. The deck scan alone cannot provide it - it only sees where a
 * track is right now, so it misses one that was loaded and then unloaded, or
 * one whose load has not published its stable_id yet.
 */
let _claimedIds = new Set<string>();
/** Deferred master promotion, retried until the follower is audible (row 18). */
let _pendingMaster: { deck: DeckId; stable_id: string } | null = null;
let _promoting = false;

/** Carries how far a handoff got, so the caller knows if a retry is legal. */
class AutoPlayHandoffError extends Error {
	readonly phase: AutoPlayHandoffPhase;
	constructor(phase: AutoPlayHandoffPhase, cause: unknown) {
		super(cause instanceof Error ? cause.message : String(cause), { cause });
		this.name = 'AutoPlayHandoffError';
		this.phase = phase;
	}
}

/** Read-only charted AutoPlay order for the open playlist (library column). */
export const autoPlayOrder = $state<{
	chain: readonly string[];
	rankOf: ReadonlyMap<string, number>;
}>({ chain: [], rankOf: new Map() });

function _publishOrder(chain: readonly string[]): void {
	if (
		chain.length === autoPlayOrder.chain.length &&
		chain.every((id, i) => id === autoPlayOrder.chain[i])
	) {
		return;
	}
	autoPlayOrder.chain = chain;
	autoPlayOrder.rankOf = new Map(chain.map((id, i) => [id, i + 1]));
}

function _refreshChartedOrder(
	source: AutoPlayDeckSnap | null,
	excludeIds: ReadonlySet<string>
): void {
	if (!uiPrefs.auto_play_enabled || source === null || source.stable_id === null) {
		_publishOrder([]);
		return;
	}
	_syncPlayedSet();
	const bounds = tempoBoundsFromPitchRange(pitchRanges[source.id] ?? 16);
	const full = simulateAutoPlayChain({
		playlist: getAutoPlayPlaylist(),
		start_stable_id: source.stable_id,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		exclude_ids: excludeIds,
		played_ids: _playedIds
	});
	// Upcoming only - source is already playing, not "next".
	_publishOrder(full.slice(1));
}

function _snaps(): AutoPlayDeckSnap[] {
	return DECK_IDS.map((id) => {
		const d = deckStates[id];
		return {
			id,
			stable_id: d.stable_id,
			playing: d.playing,
			position_ms: d.position_ms,
			duration_ms: d.duration_ms,
			is_master: d.is_master,
			beat_sync_enabled: d.beat_sync_enabled
		};
	});
}

function _excludeIds(sourceId: DeckId, snaps: readonly AutoPlayDeckSnap[]): Set<string> {
	return autoPlayExcludedIds({
		decks: snaps,
		source_deck: sourceId,
		claimed_ids: _claimedIds,
		unplayable_ids: _unplayableIds
	});
}

function _syncPlayedSet(): void {
	const epoch = getAutoPlayFeedEpoch();
	if (epoch !== _playedFeedEpoch) {
		// Played history and quarantine are playlist-scoped, so a membership
		// change retires them. _claimedIds is NOT retired: a track AutoPlay
		// already put on a deck must stay unpickable regardless of the feed.
		_playedIds = new Set();
		_unplayableIds = new Set();
		_playedFeedEpoch = epoch;
	}
}

function _gridOrNull(deck: DeckId): readonly AnlzBeat[] | null {
	const beats = deckStates[deck].anlz?.beatgrid.beats;
	try {
		validateBeatGrid(beats ?? []);
	} catch {
		return null;
	}
	return beats ?? null;
}

/** Pure plan probe using live decks; never mutates transport. */
function _phaseLockOk(sourceId: DeckId, follower: DeckId): { ok: true } | { ok: false; error: string } {
	const masterGrid = _gridOrNull(sourceId);
	const followerGrid = _gridOrNull(follower);
	if (masterGrid === null) {
		return { ok: false, error: `source deck ${sourceId} has no valid real PQTZ beat grid` };
	}
	if (followerGrid === null) {
		return { ok: false, error: `follower deck ${follower} has no valid real PQTZ beat grid` };
	}
	const bounds = tempoBoundsFromPitchRange(pitchRanges[follower]);
	const masterPosSec = Math.max(0, deckStates[sourceId].position_ms / 1000);
	const rawFollowerSec = Math.max(0, deckStates[follower].position_ms / 1000);
	const followerPositionSec = quantizeToNearestBeat(followerGrid, rawFollowerSec);
	const masterTempoRatio = deckStates[sourceId].pitch;
	const mode = deckStates[follower].sync_mode;
	try {
		computeFollowerSyncPlan({
			masterGrid,
			followerGrid,
			masterPositionAtSyncSec: masterPosSec,
			masterTempoRatio,
			followerPositionSec,
			currentContextTimeSec: 0,
			syncAtContextTimeSec: PREFLIGHT_SYNC_AHEAD_SEC,
			minFollowerTempoRatio: bounds.min,
			maxFollowerTempoRatio: bounds.max,
			mode
		});
		return { ok: true };
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		return { ok: false, error: message };
	}
}

async function _applyBeatSyncDecision(
	source: AutoPlayDeckSnap,
	follower: DeckId
): Promise<void> {
	const probe = source.beat_sync_enabled
		? _phaseLockOk(source.id, follower)
		: { ok: false as const, error: 'source Beat Sync off' };
	const decision = decideAutoPlayBeatSync({
		source_beat_sync_enabled: source.beat_sync_enabled,
		phase_lock_ok: probe.ok
	});
	const currentlyOn = deckStates[follower].beat_sync_enabled;
	if (decision === 'enable' && !currentlyOn) {
		await dispatchPerformanceCommand({ type: 'beat_sync', deck: follower, enabled: true });
	} else if (decision === 'disable' && currentlyOn) {
		await dispatchPerformanceCommand({ type: 'beat_sync', deck: follower, enabled: false });
	}
	if (source.beat_sync_enabled && !probe.ok) {
		const bounds = tempoBoundsFromPitchRange(pitchRanges[follower]);
		pushToast(
			formatAutoPlaySyncSkipToast({
				follower_deck: follower,
				mode: deckStates[follower].sync_mode,
				plan_error: probe.error,
				min_ratio: bounds.min,
				max_ratio: bounds.max
			}),
			'info'
		);
	}
}

/**
 * Load + start the follower, then hand it the master role.
 *
 * Two phases, split at the moment the track lands on the deck (matrix rows
 * 16/17). Everything before is retryable with a different candidate; nothing
 * after is, because the operator now has a loaded deck and a second load would
 * be exactly the duplicate this module exists to prevent.
 */
async function _handoff(source: AutoPlayDeckSnap, follower: DeckId, nextId: string): Promise<void> {
	try {
		const occupied = deckStates[follower].stable_id;
		if (occupied !== null && occupied !== nextId) {
			await dispatchPerformanceCommand({ type: 'unload', deck: follower });
		}
		if (deckStates[follower].stable_id !== nextId) {
			await dispatchPerformanceCommand({ type: 'load', deck: follower, stable_id: nextId });
		}
	} catch (error: unknown) {
		throw new AutoPlayHandoffError('load', error);
	}
	// ---- commit point: nextId is on deck `follower` from here down ----
	try {
		await _applyBeatSyncDecision(source, follower);
		await dispatchPerformanceCommand({ type: 'play', deck: follower, playing: true });
	} catch (error: unknown) {
		throw new AutoPlayHandoffError('commit', error);
	}
	// pickSourceDeck only ever arms off the master, so AutoPlay must move
	// master to the deck it just started - otherwise the next tick still sees
	// the (now trailing/finished) old master as source and stalls.
	//
	// Deferred, not awaited here (row 18): `play` only SCHEDULES the transport,
	// while `audible` is published later by the presented-transport
	// observation. Asking for master in this microtask therefore hit
	// "setDeckMaster: cannot select paused deck N while decks [...] are audible
	// or scheduled to play" on nearly every handoff - the outgoing master is
	// audible by definition at handoff time. _promoteMaster retries once the
	// follower is actually presenting audio.
	_pendingMaster = { deck: follower, stable_id: nextId };
}

/** Idempotent, single-shot per pending promotion; safe to call every tick. */
async function _promoteMaster(): Promise<void> {
	if (_promoting) return;
	const pending = _pendingMaster;
	if (pending === null) return;
	const snap = _snaps().find((d) => d.id === pending.deck) ?? null;
	const decision = decideMasterPromotion({
		pending_deck: pending.deck,
		pending_stable_id: pending.stable_id,
		deck: snap,
		audible: deckStates[pending.deck].audible
	});
	if (decision === 'wait') return;
	if (decision === 'drop') {
		_pendingMaster = null;
		return;
	}
	if (decision === 'promote') {
		_pendingMaster = null;
		_promoting = true;
		try {
			await dispatchPerformanceCommand({ type: 'master', deck: pending.deck });
		} catch (error: unknown) {
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`auto-play: deck ${pending.deck} master handover refused: ${message}`, 'error');
		} finally {
			_promoting = false;
		}
		return;
	}
	const _exhaustive: never = decision;
	throw new Error(`unhandled master promotion decision: ${String(_exhaustive)}`);
}

async function _tick(): Promise<void> {
	if (!uiPrefs.auto_play_enabled) {
		_publishOrder([]);
		_pendingMaster = null;
		return;
	}
	// Row 18: runs on every poll, including while a handoff is in flight.
	await _promoteMaster();
	if (_inFlight) return;

	const snaps = _snaps();
	const source = pickSourceDeck(snaps);
	if (source === null || source.stable_id === null) {
		// Row 1, but NOT while a promotion is still settling: between the old
		// master being demoted and the new one being flagged there is a poll or
		// two with no master at all. Disarming there would let the outgoing
		// track - still inside its window - arm a second time and hand off
		// again. Only a genuine idle state clears the arm.
		if (_pendingMaster === null) {
			_triggeredFor = null;
			_waitingFollowerFor = null;
		}
		_publishOrder([]);
		return;
	}

	_syncPlayedSet();
	const excludeIds = _excludeIds(source.id, snaps);
	_refreshChartedOrder(source, excludeIds);

	const rem = remainingMs(source.position_ms, source.duration_ms);
	if (rem !== null && rem > AUTO_PLAY_THRESHOLD_MS) {
		// Seek back out of the window: allow a later re-arm for same track.
		if (_triggeredFor === source.stable_id) _triggeredFor = null;
		if (_waitingFollowerFor === source.stable_id) _waitingFollowerFor = null;
		return;
	}

	if (
		!shouldTriggerAutoPlay({
			enabled: true,
			remaining_ms: rem,
			threshold_ms: AUTO_PLAY_THRESHOLD_MS,
			source_stable_id: source.stable_id,
			already_triggered_for: _triggeredFor,
			in_flight: _inFlight
		})
	) {
		return;
	}

	const follower = pickFollowerDeck(snaps, source.id);
	if (follower === null) {
		// Do not pin _triggeredFor - retry when a deck frees; toast once.
		if (_waitingFollowerFor !== source.stable_id) {
			pushToast('auto-play: no free/stopped follower deck', 'error');
			_waitingFollowerFor = source.stable_id;
		}
		return;
	}
	_waitingFollowerFor = null;

	const bounds = tempoBoundsFromPitchRange(pitchRanges[follower]);
	const sourceDeck = deckStates[source.id];
	const nextId = pickNextStableId({
		playlist: getAutoPlayPlaylist(),
		current_stable_id: source.stable_id,
		current_key: sourceDeck.key,
		current_bpm: sourceDeck.bpm,
		exclude_ids: excludeIds,
		played_ids: _playedIds,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach
	});
	if (nextId === null) {
		const remaining = getAutoPlayPlaylist().filter(
			(r) =>
				r.stable_id !== source.stable_id &&
				!excludeIds.has(r.stable_id) &&
				!_playedIds.has(r.stable_id)
		);
		const allMissing = remaining.length > 0 && remaining.every((r) => !r.file_exists);
		pushToast(
			allMissing
				? 'auto-play: remaining playlist tracks are missing/stub audio'
				: uiPrefs.auto_play_enforce_order
					? 'auto-play: no next unplayed track in playlist order'
					: 'auto-play: no unplayed playlist track within key +-1 and Beat Sync BPM range',
			'error'
		);
		_triggeredFor = source.stable_id;
		return;
	}

	// Row 4/c: arm and claim BEFORE dispatching anything. Both are one-way for
	// the life of this source track. Recording them only on success is what let
	// a late failure roll the trigger back and load a second track.
	_inFlight = true;
	_triggeredFor = source.stable_id;
	_claimedIds.add(nextId);
	_playedIds.add(source.stable_id);
	_playedIds.add(nextId);
	try {
		await _handoff(source, follower, nextId);
		_attemptsFor = { source: '', count: 0 };
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		// An unclassified throw is treated as committed. That is the safe
		// direction: a wrong 'load' guess would re-arm and load a second track,
		// which is the live bug. A wrong 'commit' guess only forgoes a retry.
		const phase: AutoPlayHandoffPhase =
			error instanceof AutoPlayHandoffError ? error.phase : 'commit';
		if (!handoffFailureIsRetryable(phase)) {
			// Row 17: nextId IS on the follower. Do not quarantine it, do not
			// re-arm - _triggeredFor stays pinned to this source track.
			pushToast(
				`auto-play: deck ${follower} loaded ${nextId.slice(0, 12)}... but the handoff ` +
					`did not finish: ${message}`,
				'error'
			);
			return;
		}
		// Row 16: nothing landed on the deck; quarantine and try another pick.
		_unplayableIds.add(nextId);
		if (_attemptsFor.source !== source.stable_id) {
			_attemptsFor = { source: source.stable_id, count: 0 };
		}
		_attemptsFor.count += 1;
		if (_attemptsFor.count < MAX_HANDOFF_ATTEMPTS) {
			_triggeredFor = null;
			pushToast(`auto-play: skipped unplayable (${nextId.slice(0, 12)}...): ${message}`, 'info');
		} else {
			pushToast(
				`auto-play stopped after ${MAX_HANDOFF_ATTEMPTS} failed handoffs: ${message}`,
				'error'
			);
		}
	} finally {
		_inFlight = false;
	}
}

/** Start polling; returns uninstall that clears the timer. */
export function installAutoPlay(): () => void {
	if (_timer !== null) {
		throw new Error('auto-play already installed');
	}
	_timer = setInterval(() => {
		void _tick();
	}, POLL_MS);
	return () => {
		if (_timer !== null) {
			clearInterval(_timer);
			_timer = null;
		}
		_inFlight = false;
		_triggeredFor = null;
		_waitingFollowerFor = null;
		_pendingMaster = null;
		_promoting = false;
		_claimedIds = new Set();
		_playedIds = new Set();
		_unplayableIds = new Set();
		_attemptsFor = { source: '', count: 0 };
		_playedFeedEpoch = -1;
		_publishOrder([]);
	};
}
