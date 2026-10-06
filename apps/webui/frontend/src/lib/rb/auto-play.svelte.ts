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
	decideMasterPromotion,
	effectiveAutoPlayThresholdMs,
	handoffFailureIsRetryable,
	getAutoPlayFeedEpoch,
	getAutoPlayPlaylist,
	registerAutoPlayRankProvider,
	remainingAutoPlayCandidates,
	pickFollowerDeck,
	pickNextStableId,
	pickSourceDeck,
	remainingMs,
	shouldTriggerAutoPlay,
	tempoBoundsFromPitchRange,
	type AutoPlayDeckSnap,
	type AutoPlayHandoffPhase,
	type AutoPlayMasterPromotion
} from '$lib/rb/auto-play';
import { clearChartedAutoPlayOrder, deckSongIdentities, refreshChartedAutoPlayOrder } from '$lib/rb/auto-play-chart-order';
import {
	isAutoPlayArmedEmptyActive,
	noteAutoPlayArmedEmptyOnEnable,
	noteAutoPlayPlaybackStarted,
	trackAutoPlayIdleClock,
	readAutoPlayIdleSinceMs,
	resetAutoPlayIdleClock,
	shouldRaiseAutoPlayNoMasterStall,
	shouldRaiseAutoPlaySilentStall
} from '$lib/rb/autoplay-idle';
import {
	isSilenceRecovering,
	pickPausedMasterSource,
	noteAutoPlayFollowerPlayDispatched,
	resetAutoPlaySilenceRecovery,
	resolveAutoPlaySourceForTick
} from '$lib/rb/autoplay-silence-recover';
import { clearEngineRecoveryStop, engineRecoveryStopReason } from '$lib/rb/engine-recovery-stop';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';
import {
	activateAutoPlayQueue,
	autoPlayOrder,
	clearAutoPlayOrder,
	clearAutoPlayQueue,
} from '$lib/rb/autoplay-queue.svelte';
import { autoPlayExhaustionToast, autoPlayStallReason } from '$lib/rb/autoplay-stall';
import { applyAutoPlayBeatSyncDecision } from '$lib/rb/auto-play-phase-lock';
import {
	clearAutoPlayStall,
	noteAutoPlayExhaustion,
	noteAutoPlayHandoffStall,
	noteAutoPlayNoPlayingMaster,
	noteAutoPlaySilentIdle,
	readAutoPlayStall,
	retireAutoPlayStallIfAudible
} from '$lib/rb/autoplay-stall.svelte';
import { autoPlayDeckSnaps, autoPlayExcludeIds } from '$lib/rb/auto-play-snap';
import { AutoPlayHandoffError } from '$lib/rb/auto-play-handoff-error';
import { runAutoPlayHandoff } from '$lib/rb/auto-play-handoff';
import type { DeckId } from '$lib/rb/deck-slots';
import { classifyAutoPlayStatus, type AutoPlayMirrorStatus } from '$lib/rb/autoplay-status';

const POLL_MS = 250;
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
const _chartKeyRef = { current: null as string | null };
let _unplayableIds = new Set<string>();
/**
 * Candidates that failed to LOAD for the current source, by id.
 *
 * The ids, not a count (Codex r3974580403): a failed candidate is quarantined
 * in `_unplayableIds` and therefore drops out of `remaining`, so a stall built
 * from `remaining` named the surviving INCOMPATIBLE rows as the files to check
 * while the ones that actually failed lived only in an expiring toast. It is
 * also source-scoped and feed-scoped: `_syncPlayedSet` retires it with the rest
 * of the playlist-scoped state, or a new playlist inherits the last one's
 * failures and is told its own candidates failed to load (r3974580407).
 */
let _attemptsFor: { source: string; failed_ids: string[] } = { source: '', failed_ids: [] };
/** Toast-once while waiting for a free follower (does not pin _triggeredFor). */
let _waitingFollowerFor: string | null = null;
/** Empty-feed toast epoch during playlist hydration, without consuming the source arm. */
let _waitingEmptyFeedEpoch: number | null = null;
/** IDs claimed before dispatch, never retired by a feed epoch, prevent duplicate loads. */
let _claimedIds = new Set<string>();
/** Deferred master promotion, retried until the follower is audible (row 18). */
let _pendingMaster: { deck: DeckId; stable_id: string } | null = null;
let _promoting = false;
/** Last pickSourceDeck id, used when every deck has stopped (issue #2153). */
let _lastSourceStableId: string | null = null;
/** PLAY-15: since when a deck has been playing with no playing master. */
let _noMasterSinceMs: number | null = null;

export { autoPlayOrder } from '$lib/rb/autoplay-queue.svelte';

export function readAutoPlayHandoffInFlight(): boolean {
	return _inFlight;
}

/**
 * AGENT-20: what the UI-mirror publishes about AutoPlay. Read-only: it uses
 * the same source picks as `_tick` but never the resolver, which clears the
 * silence-recovery flag as a side effect.
 */
export function readAutoPlayMirrorStatus(): AutoPlayMirrorStatus {
	const snaps = _snaps();
	const source =
		pickSourceDeck(snaps) ?? (isSilenceRecovering() ? pickPausedMasterSource(snaps) : null);
	return classifyAutoPlayStatus({
		enabled: uiPrefs.auto_play_enabled,
		installed: _stopArmWatcher !== null,
		stall_reason: readAutoPlayStall()?.reason ?? null,
		has_source: source !== null && source.stable_id !== null,
		handoff_pending: _inFlight || _pendingMaster !== null,
		any_playing: snaps.some((deck) => deck.playing)
	});
}
function _clearChartedOrder(): void {
	clearChartedAutoPlayOrder(_chartKeyRef);
}

function _refreshChartedOrder(
	source: AutoPlayDeckSnap | null,
	snaps: readonly AutoPlayDeckSnap[],
	excludeIds: ReadonlySet<string>
): void {
	refreshChartedAutoPlayOrder({
		source,
		snaps,
		excludeIds,
		playedFeedEpoch: _playedFeedEpoch,
		playedIds: _playedIds,
		chartKey: _chartKeyRef,
		syncPlayedSet: _syncPlayedSet
	});
}

/**
 * Which arming this is. Bumped on every install, arm and disarm edge.
 *
 * A handoff is several awaits long and can reject after the operator toggled
 * AutoPlay off and on again, or left /performance and came back. "Is AutoPlay
 * on right now" answers yes for the REPLACEMENT session, which would let a
 * dead handoff restore its stale stall into it (Codex r3974381587), so a late
 * failure has to prove it belongs to the arming it started under.
 */
let _generation = 0;

/** Same arming, still armed, still installed. */
function _armedAt(generation: number): boolean {
	return uiPrefs.auto_play_enabled && _stopArmWatcher !== null && generation === _generation;
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
		_attemptsFor = { source: '', failed_ids: [] };
		_playedFeedEpoch = epoch;
		if (_exhaustedFeedEpoch !== null) {
			_triggeredFor = null;
			_exhaustedFeedEpoch = null;
		}
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
async function _handoff(
	source: AutoPlayDeckSnap,
	follower: DeckId,
	nextId: string,
	generation: number
): Promise<void> {
	const outcome = await runAutoPlayHandoff(nextId, {
		stillArmed: () => _armedAt(generation),
		followerStableId: () => deckStates[follower].stable_id,
		unload: () => dispatchPerformanceCommand({ type: 'unload', deck: follower }, undefined, 'autoplay-handoff'),
		load: () =>
			dispatchPerformanceCommand({ type: 'load', deck: follower, stable_id: nextId }, undefined, 'autoplay-handoff'),
		beatSync: () => applyAutoPlayBeatSyncDecision(source, follower),
		play: () =>
			dispatchPerformanceCommand({ type: 'play', deck: follower, playing: true }, undefined, 'autoplay-handoff'),
		notifySyncSkip: (message) => pushToast(message, 'info'),
		onPlayDispatched: noteAutoPlayFollowerPlayDispatched
	});
	if (outcome === 'abandoned-disarmed') {
		console.info(
			`[autoplay] handoff abandoned: AutoPlay was switched off mid-handoff (deck ${follower}, ${nextId.slice(0, 12)})`
		);
		return;
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
		resetAutoPlaySilenceRecovery();
		return;
	}
	if (decision === 'promote') {
		_pendingMaster = null;
		_promoting = true;
		const generation = _generation;
		try {
			await dispatchPerformanceCommand({ type: 'master', deck: pending.deck }, undefined, 'autoplay-handoff');
		} catch (error: unknown) {
			if (!_armedAt(generation)) return;
			const message = error instanceof Error ? error.message : String(error);
			pushToast(`auto-play: deck ${pending.deck} master handover refused: ${message}`, 'error');
			// PLAY-08 (r3974888765): playing, not master, so nothing queues
			// after it. Terminal, and it was only ever an expiring toast.
			noteAutoPlayHandoffStall('master-handover-refused', pending.stable_id, message, true);
		} finally {
			_promoting = false;
		}
		return;
	}
	const _exhaustive: never = decision;
	throw new Error(`unhandled master promotion decision: ${String(_exhaustive)}`);
}

/**
 * PLAY-15: a deck playing with no playing master leaves AutoPlay blind, since
 * pickSourceDeck only arms off the master. Say so while the music is still
 * going, not 30 s after it ends (Mon 5 Oct 2026, silver preview).
 */
function _noteNoPlayingMaster(noSource: boolean, snaps: readonly AutoPlayDeckSnap[]): void {
	const playingDeck = snaps.find((d) => d.playing && d.stable_id !== null) ?? null;
	if (!noSource || playingDeck === null || _pendingMaster !== null) {
		_noMasterSinceMs = null;
		return;
	}
	const now = Date.now();
	_noMasterSinceMs ??= now;
	if (
		shouldRaiseAutoPlayNoMasterStall({
			enabled: uiPrefs.auto_play_enabled,
			any_playing: true,
			playing_master: false,
			pending_master: false,
			stall_active: readAutoPlayStall() !== null,
			no_master_since_ms: _noMasterSinceMs,
			now_ms: now
		})
	) {
		noteAutoPlayNoPlayingMaster(playingDeck.stable_id!);
	}
}

async function _tick(): Promise<void> {
	if (!uiPrefs.auto_play_enabled) {
		_clearChartedOrder();
		_pendingMaster = null;
		resetAutoPlayIdleClock();
		return;
	}
	// Row 18: runs on every poll, including while a handoff is in flight.
	await _promoteMaster();
	if (_inFlight) return;

	const snaps = _snaps();
	const anyPlaying = snaps.some((d) => d.playing);
	noteAutoPlayArmedEmptyOnEnable({
		enabled: uiPrefs.auto_play_enabled,
		any_playing: anyPlaying,
		now_ms: Date.now()
	});
	if (anyPlaying) {
		noteAutoPlayPlaybackStarted();
		// Bug #58: a deck playing again ends any engine-recovery stop.
		clearEngineRecoveryStop();
	}
	// PLAY-18: idle only advances the stall clock. It never writes the
	// operator's toggle; the mirror says armed=false, no-deck-playing.
	trackAutoPlayIdleClock({
		enabled: uiPrefs.auto_play_enabled,
		snaps,
		pending_master: _pendingMaster !== null,
		silence_recovering: isSilenceRecovering(),
		engine_recovery_stop: engineRecoveryStopReason(),
		now_ms: Date.now()
	});

	const sourceStableId =
		_lastSourceStableId ?? snaps.find((d) => d.stable_id !== null)?.stable_id ?? null;
	if (
		shouldRaiseAutoPlaySilentStall({
			enabled: uiPrefs.auto_play_enabled,
			any_playing: anyPlaying,
			pending_master: _pendingMaster !== null,
			silence_recovering: isSilenceRecovering(),
			engine_recovery_stop: engineRecoveryStopReason(),
			stall_active: readAutoPlayStall() !== null,
			idle_since_ms: readAutoPlayIdleSinceMs(),
			now_ms: Date.now(),
			source_stable_id: sourceStableId,
			armed_empty_active: isAutoPlayArmedEmptyActive()
		})
	) {
		noteAutoPlaySilentIdle({ source_stable_id: sourceStableId!, blocked: [] });
	}

	const resolved = resolveAutoPlaySourceForTick(snaps, pickSourceDeck);
	const source = resolved.source;
	if (source !== null && source.stable_id !== null) {
		_lastSourceStableId = source.stable_id;
	}
	_noteNoPlayingMaster(source === null || source.stable_id === null, snaps);
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
	retireAutoPlayStallIfAudible(source.stable_id, deckStates[source.id].audible);
	const excludeIds = autoPlayExcludeIds(source.id, snaps, _claimedIds, _unplayableIds);
	_refreshChartedOrder(source, snaps, excludeIds);

	const rem =
		resolved.remainingOverride ?? remainingMs(source.position_ms, source.duration_ms);
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
		maximize_reach: !uiPrefs.auto_play_enforce_order && uiPrefs.auto_play_maximize_reach,
		exclude_identities: deckSongIdentities()
	});
	if (nextId === null) {
		const feed = getAutoPlayPlaylist();
		const remaining = remainingAutoPlayCandidates({
			playlist: feed, current_stable_id: source.stable_id,
			exclude_ids: excludeIds, played_ids: _playedIds
		});
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
		// ONE derivation for the toast and the durable state: the toast is what
		// reaches webui-client-errors-*.log, so two derivations means the
		// incident row and the screen can name different causes (r3974518065).
		const reason = autoPlayStallReason({
			all_missing: allMissing,
			load_failures: _attemptsFor.source === source.stable_id && _attemptsFor.failed_ids.length > 0,
			enforce_order: uiPrefs.auto_play_enforce_order
		});
		pushToast(autoPlayExhaustionToast(reason), 'error');
		// The tracks worth NAMING differ by cause: for a load failure they are
		// the quarantined candidates, which `remaining` has already excluded.
		const blocked = reason === 'candidates-failed-to-load'
			? feed.filter((row) => _attemptsFor.failed_ids.includes(row.stable_id))
			: remaining;
		// PLAY-08: the toast above expires. Issue #1640 is that nothing outlived it.
		noteAutoPlayExhaustion({ source_stable_id: source.stable_id, reason, blocked });
		_triggeredFor = source.stable_id;
		_exhaustedFeedEpoch = _playedFeedEpoch;
		return;
	}

	// Row 4/c: arm and claim BEFORE dispatching anything. Both are one-way for
	// the life of this source track. Recording them only on success is what let
	// a late failure roll the trigger back and load a second track.
	const generation = _generation;
	_inFlight = true;
	_triggeredFor = source.stable_id;
	_exhaustedFeedEpoch = null;
	_waitingEmptyFeedEpoch = null;
	_claimedIds.add(nextId);
	_playedIds.add(source.stable_id);
	_playedIds.add(nextId);
	try {
		await _handoff(source, follower, nextId, generation);
		_attemptsFor = { source: '', failed_ids: [] };
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		// NOTHING from a dead arming may report or mutate (Codex r3974734066,
		// r3974734057). The guard is here, at the top, rather than on the
		// durable raise alone: `pushToast` writes the perf ring AND posts to
		// /api/v1/client-errors, so a stale rejection was still filing a
		// server-side failure against a session that had ended; and the
		// quarantine below would make the REPLACEMENT session skip a candidate
		// it never tried, then blame it for a load failure it never had.
		//
		// Safe to abandon: every field this would have touched is module-level
		// state that teardown and the arm effect have already reset, and
		// `_inFlight` is cleared in `finally` either way.
		if (!_armedAt(generation)) return;
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
			noteAutoPlayHandoffStall('handoff-incomplete', source.stable_id, message, _armedAt(generation));
			return;
		}
		// Row 16: nothing landed on the deck; quarantine and try another pick.
		_unplayableIds.add(nextId);
		if (_attemptsFor.source !== source.stable_id) {
			_attemptsFor = { source: source.stable_id, failed_ids: [] };
		}
		// Distinct by construction: nextId went into _claimedIds before dispatch.
		_attemptsFor.failed_ids.push(nextId);
		if (_attemptsFor.failed_ids.length < MAX_HANDOFF_ATTEMPTS) {
			_triggeredFor = null;
			pushToast(`auto-play: skipped unplayable (${nextId.slice(0, 12)}...): ${message}`, 'info');
		} else {
			pushToast(
				`auto-play stopped after ${MAX_HANDOFF_ATTEMPTS} failed handoffs: ${message}`,
				'error'
			);
			const failed = getAutoPlayPlaylist().filter((row) =>
				_attemptsFor.failed_ids.includes(row.stable_id)
			);
			noteAutoPlayHandoffStall(
				'handoff-attempts-exhausted', source.stable_id, message, _armedAt(generation), failed
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
			_generation += 1;
			if (uiPrefs.auto_play_enabled) {
				// PLAY-05: activation creates the inspectable queue before the
				// first poll has a master track from which to calculate handoffs.
				activateAutoPlayQueue();
				_chartKeyRef.current = null;
				clearAutoPlayOrder();
				_startPoll();
			} else {
				_stopPoll();
				_chartKeyRef.current = null;
				clearAutoPlayOrder();
				clearAutoPlayQueue();
				// PLAY-18: only a user turns AutoPlay off, so a stall has
				// nothing left to explain once they have.
				clearAutoPlayStall();
			}
		});
	});
	return () => {
		_generation += 1;
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
		resetAutoPlayIdleClock();
		resetAutoPlaySilenceRecovery();
		_noMasterSinceMs = null;
		_claimedIds = new Set();
		_playedIds = new Set();
		_unplayableIds = new Set();
		_attemptsFor = { source: '', failed_ids: [] };
		_playedFeedEpoch = -1;
		_clearChartedOrder();
		clearAutoPlayQueue();
		clearAutoPlayStall();
	};
}
