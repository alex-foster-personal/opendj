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
import {
	DECK_IDS,
	deckAudioClockPositionMs,
	deckStates,
	pitchRanges
} from '$lib/rb/audio-engine.svelte';
import {
	AUTO_PLAY_THRESHOLD_MS,
	decideAutoPlayBeatSync,
	decideMasterPromotion,
	effectiveAutoPlayThresholdMs,
	handoffFailureIsRetryable,
	formatAutoPlaySyncSkipToast,
	getAutoPlayFeedEpoch,
	getAutoPlayPlaylist,
	getAutoPlayPlaylistRevision,
	registerAutoPlayRankProvider,
	pickFollowerDeck,
	pickNextStableId,
	pickSourceDeck,
	remainingMs,
	shouldTriggerAutoPlay,
	simulateAutoPlayChain,
	tempoBoundsFromPitchRange,
	type AutoPlayDeckSnap,
	type AutoPlayHandoffPhase,
	type AutoPlayMasterPromotion,
	chartedOrderKey
} from '$lib/rb/auto-play';
import {
	computeFollowerSyncPlan,
	quantizeToNearestBeat,
	validateBeatGrid
} from '$lib/rb/beat-sync-math';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';
import {
	activateAutoPlayQueue,
	autoPlayOrder,
	clearAutoPlayOrder,
	clearAutoPlayQueue,
	publishAutoPlayOrder
} from '$lib/rb/autoplay-queue.svelte';
import { describeAutoPlayStall, type AutoPlayStallReason } from '$lib/rb/autoplay-stall';
import {
	clearAutoPlayStall,
	raiseAutoPlayStall,
	readAutoPlayStall
} from '$lib/rb/autoplay-stall.svelte';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { autoPlayDeckSnaps, autoPlayExcludeIds } from '$lib/rb/auto-play-snap';
import { AutoPlayHandoffError } from '$lib/rb/auto-play-handoff-error';
import type { DeckId } from '$lib/rb/deck-slots';

const POLL_MS = 250;
/**
 * Rows of upcoming order the library column charts. The picker itself only ever
 * needs the next track; walking the whole playlist under maximize_reach cost
 * 6.9 s per simulation on 8558 rows (Wed 2 Sep 2026), so the column shows the
 * near future and ranks beyond it are simply absent.
 */
const CHARTED_ORDER_HORIZON = 64;
/** Synthetic schedule horizon for preflight only (plan needs syncAt > now). */
const PREFLIGHT_SYNC_AHEAD_SEC = 0.05;
/** Failed load/play attempts per source track before stopping. */
const MAX_HANDOFF_ATTEMPTS = 3;

let _timer: ReturnType<typeof setInterval> | null = null;
/** Teardown for the $effect.root that arms/disarms the poll. Install marker. */
let _stopArmWatcher: (() => void) | null = null;
let _inFlight = false;
let _triggeredFor: string | null = null;
let _playedIds = new Set<string>();
let _playedFeedEpoch = -1;
/** Epoch whose non-empty feed exhausted this source without committing a load. */
let _exhaustedFeedEpoch: number | null = null;
/** Inputs of the last charted-order simulation; equal key = skip the tick's simulate. */
let _chartedOrderKey: string | null = null;
/** Candidates that failed load/play; cleared when playlist membership changes. */
let _unplayableIds = new Set<string>();
let _attemptsFor: { source: string; count: number } = { source: '', count: 0 };
/** Toast-once while waiting for a free follower (does not pin _triggeredFor). */
let _waitingFollowerFor: string | null = null;
/** Empty-feed toast epoch during playlist hydration, without consuming the source arm. */
let _waitingEmptyFeedEpoch: number | null = null;
/** IDs claimed before dispatch, never retired by a feed epoch, prevent duplicate loads. */
let _claimedIds = new Set<string>();
/** Deferred master promotion, retried until the follower is audible (row 18). */
let _pendingMaster: { deck: DeckId; stable_id: string } | null = null;
let _promoting = false;

export { autoPlayOrder } from '$lib/rb/autoplay-queue.svelte';

/** Clear a published chart and its memo together, so recovery always re-plans. */
function _clearChartedOrder(): void {
	_chartedOrderKey = null;
	publishAutoPlayOrder([]);
}

function _refreshChartedOrder(
	source: AutoPlayDeckSnap | null,
	snaps: readonly AutoPlayDeckSnap[],
	excludeIds: ReadonlySet<string>
): void {
	if (!uiPrefs.auto_play_enabled || source === null || source.stable_id === null) {
		_clearChartedOrder();
		return;
	}
	_syncPlayedSet();
	const follower = pickFollowerDeck(snaps, source.id);
	if (follower === null) {
		_clearChartedOrder();
		return;
	}
	const followerPitchRange = pitchRanges[follower];
	const bounds = tempoBoundsFromPitchRange(followerPitchRange);
	// The poll fires every 250 ms; simulating the chain over the open playlist
	// is O(rows^2) (1.5 s on 8558 rows). Nothing about the order can change
	// unless one of these inputs did, so an unchanged key is a no-op tick.
	const key = chartedOrderKey({
		feed_epoch: _playedFeedEpoch,
		playlist_revision: getAutoPlayPlaylistRevision(),
		source_stable_id: source.stable_id,
		source_key: deckStates[source.id].key,
		source_bpm: deckStates[source.id].bpm,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		exclude_ids: excludeIds,
		played_ids: _playedIds,
		follower_deck: follower,
		follower_pitch_range: followerPitchRange
	});
	if (key === _chartedOrderKey) return;
	_chartedOrderKey = key;
	const full = simulateAutoPlayChain({
		select_next: pickNextStableId,
		playlist: getAutoPlayPlaylist(),
		start_stable_id: source.stable_id,
		start_key: deckStates[source.id].key,
		start_bpm: deckStates[source.id].bpm,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		exclude_ids: excludeIds,
		played_ids: _playedIds,
		max_chain_length: CHARTED_ORDER_HORIZON + 1
	});
	// Upcoming only - source is already playing, not "next".
	publishAutoPlayOrder(full.slice(1));
}

function _snaps(): AutoPlayDeckSnap[] {
	return autoPlayDeckSnaps(DECK_IDS, (id) => deckStates[id], deckAudioClockPositionMs);
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
		if (_exhaustedFeedEpoch !== null) {
			_triggeredFor = null;
			_exhaustedFeedEpoch = null;
		}
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
	const decision: AutoPlayMasterPromotion = decideMasterPromotion({
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
		_clearChartedOrder();
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
			_exhaustedFeedEpoch = null;
			_waitingFollowerFor = null;
			_waitingEmptyFeedEpoch = null;
		}
		_clearChartedOrder();
		return;
	}

	_syncPlayedSet();
	// PLAY-08: sound is back. A source that is AUDIBLE on a track that is NOT
	// the one AutoPlay gave up on means playback was restored - by the operator
	// loading something, or by a later handoff committing - so the recorded
	// stall is history rather than the state the room is in. Nothing else
	// retires it: the stalled track ENDING is the moment the room goes quiet,
	// which is exactly when the explanation has to still be on screen.
	//
	// `audible`, NOT `playing` (Codex r3973806301). `playing` is written
	// optimistically the moment a play is REQUESTED, while `audible` is
	// published from the presented-transport observation, i.e. from output that
	// actually happened. Gating on `playing` meant that starting another track
	// while the output device was dead deleted the explanation on the next poll
	// with the room still silent - which is the whole failure class this banner
	// exists for, reintroduced by its own clear rule.
	const stall = readAutoPlayStall();
	if (
		stall !== null &&
		source.stable_id !== stall.source_stable_id &&
		deckStates[source.id].audible
	) {
		clearAutoPlayStall();
	}
	const excludeIds = autoPlayExcludeIds(source.id, snaps, _claimedIds, _unplayableIds);
	_refreshChartedOrder(source, snaps, excludeIds);

	const rem = remainingMs(source.position_ms, source.duration_ms);
	// min(constant, duration/2): a track shorter than the constant window must
	// not arm at t=0 (it used to load the next track over its own first beat).
	const windowMs = effectiveAutoPlayThresholdMs(source.duration_ms);
	if (rem !== null && windowMs !== null && rem > windowMs) {
		// Seek back out of the window: allow a later re-arm for same track.
		if (_triggeredFor === source.stable_id) _triggeredFor = null;
		if (_triggeredFor === null) _exhaustedFeedEpoch = null;
		if (_waitingFollowerFor === source.stable_id) _waitingFollowerFor = null;
		_waitingEmptyFeedEpoch = null;
		return;
	}

	if (
		!shouldTriggerAutoPlay({
			enabled: true,
			remaining_ms: rem,
			threshold_ms: windowMs ?? AUTO_PLAY_THRESHOLD_MS,
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
		const feed = getAutoPlayPlaylist();
		const remaining = feed.filter(
			(r) =>
				r.stable_id !== source.stable_id &&
				!excludeIds.has(r.stable_id) &&
				!_playedIds.has(r.stable_id)
		);
		const allMissing = remaining.length > 0 && remaining.every((r) => !r.file_exists);
		// An empty feed is its own diagnosis and must not be reported as a
		// key/BPM dead end: on Tue 1 Sep 2026 that message sent the maintainer reading
		// key wheels while the real fault was a feed frozen before the pane
		// had any rows. Name the actual failure so the log carries it.
		if (feed.length === 0) {
			const feedEpoch = getAutoPlayFeedEpoch();
			if (_waitingEmptyFeedEpoch !== feedEpoch) {
				pushToast('auto-play: candidate feed is empty - waiting for playlist rows', 'info');
				_waitingEmptyFeedEpoch = feedEpoch;
			}
			// This is not a committed handoff. BrowserPanel deliberately publishes
			// [] while a playlist switch hydrates, then publishes the new rows with
			// a new epoch. Keep the source unarmed so that later feed can proceed.
			return;
		}
		_waitingEmptyFeedEpoch = null;
		const reason: AutoPlayStallReason = allMissing
			? 'missing-audio'
			: uiPrefs.auto_play_enforce_order
				? 'no-next-in-order'
				: 'no-compatible-track';
		pushToast(
			allMissing
					? 'auto-play: remaining playlist tracks are missing/stub audio'
					: uiPrefs.auto_play_enforce_order
						? 'auto-play: no next unplayed track in playlist order'
						: 'auto-play: no unplayed playlist track within key +-1 and Beat Sync BPM range',
			'error'
		);
		// PLAY-08: the toast above is the whole reason issue #1640 exists - it
		// clears itself after TOAST_DEFAULT_MS and the set then stops with
		// nothing on screen saying why. The stall outlives it.
		raiseAutoPlayStall(
			describeAutoPlayStall({ reason, source_stable_id: source.stable_id, blocked: remaining })
		);
		_triggeredFor = source.stable_id;
		_exhaustedFeedEpoch = _playedFeedEpoch;
		return;
	}

	// Row 4/c: arm and claim BEFORE dispatching anything. Both are one-way for
	// the life of this source track. Recording them only on success is what let
	// a late failure roll the trigger back and load a second track.
	_inFlight = true;
	_triggeredFor = source.stable_id;
	_exhaustedFeedEpoch = null;
	_waitingEmptyFeedEpoch = null;
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
			// PLAY-08: _triggeredFor stays pinned to this source for the rest of
			// its playout, so nothing else will ever start. Same terminal class
			// as exhaustion, same durable state.
			raiseAutoPlayStall(
				describeAutoPlayStall({
					reason: 'handoff-incomplete',
					source_stable_id: source.stable_id,
					blocked: [],
					detail: message
				})
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
			raiseAutoPlayStall(
				describeAutoPlayStall({
					reason: 'handoff-attempts-exhausted',
					source_stable_id: source.stable_id,
					blocked: [],
					detail: message
				})
			);
		}
	} finally {
		_inFlight = false;
	}
}

function _startPoll(): void {
	if (_timer !== null) return;
	_timer = setInterval(() => {
		void _tick();
	}, POLL_MS);
}

function _stopPoll(): void {
	if (_timer === null) return;
	clearInterval(_timer);
	_timer = null;
}

/**
 * Wire the arm watcher; returns uninstall that stops it and clears all state.
 *
 * PERFMODE-04: the poll exists only while AutoPlay is ARMED. Disarmed, _tick
 * already returned before touching a deck, but the timer still woke the main
 * thread four times a second for the life of the route, which is pure burn on
 * a machine that is meant to be protecting an audio graph. The $effect makes
 * `uiPrefs.auto_play_enabled` start and stop the interval instead, so disarmed
 * costs literally nothing and arming re-creates the same 250ms poll.
 *
 * ARMED BEHAVIOR IS UNCHANGED, deliberately: the cadence, the audio-clock read
 * in _snaps and the whole handoff path are untouched, so the background-tab
 * mixing property (78d4c95b - setInterval survives a hidden tab, and _snaps
 * reads the audio clock rather than the frozen rAF mirror) still holds.
 *
 * $effect.root because this is module-level reactive state with an explicit
 * lifecycle, not a component's - the same shape as attachMidiGlue's LED root.
 */
export function installAutoPlay(): () => void {
	if (_stopArmWatcher !== null) {
		throw new Error('auto-play already installed');
	}
	const unregisterRankProvider = registerAutoPlayRankProvider(() => autoPlayOrder.rankOf);
	_stopArmWatcher = $effect.root(() => {
		$effect(() => {
			if (uiPrefs.auto_play_enabled) {
				// PLAY-05: activation creates the inspectable queue before the
				// first poll has a master track from which to calculate handoffs.
				activateAutoPlayQueue();
				_chartedOrderKey = null;
				clearAutoPlayOrder();
				_startPoll();
			} else {
				_stopPoll();
				_chartedOrderKey = null;
				clearAutoPlayOrder();
				clearAutoPlayQueue();
				// A disarmed AutoPlay is not stalled, it is off. Leaving the
				// banner up would tell the operator a switched-off feature
				// stopped their music.
				clearAutoPlayStall();
			}
		});
	});
	return () => {
		unregisterRankProvider();
		_stopArmWatcher?.();
		_stopArmWatcher = null;
		_stopPoll();
		_inFlight = false;
		_triggeredFor = null;
		_exhaustedFeedEpoch = null;
		_waitingFollowerFor = null;
		_waitingEmptyFeedEpoch = null;
		_pendingMaster = null;
		_promoting = false;
		_claimedIds = new Set();
		_playedIds = new Set();
		_unplayableIds = new Set();
		_attemptsFor = { source: '', count: 0 };
		_playedFeedEpoch = -1;
		_clearChartedOrder();
		clearAutoPlayQueue();
		clearAutoPlayStall();
	};
}
