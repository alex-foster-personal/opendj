/**
 * /performance auto-play controller (v1 hard-cut).
 *
 * When prefs.auto_play_enabled: as the master (or playing) deck enters the
 * remaining-time window, load the next playlist track onto a free or stopped
 * follower and start it via performance-ipc. Default pick: earliest un-played
 * membership row with Camelot key +-1 and BPM inside Beat Sync pitch bounds
 * (optional maximize-reach / Warnsdorff slack). Optional enforce_play_order
 * walks strict playlist order after current.
 * Beat Sync is requested only when a real BAR/BEAT phase-lock plan succeeds;
 * otherwise follower Beat Sync is explicitly disabled and play continues
 * free-tempo (see .planning/beat-sync-phase-lock-explainer-SA.md).
 *
 * Failed handoffs quarantine the candidate and re-arm (bounded) so ghost
 * tracks / mapping 404s cannot permanently disarm AutoPlay for the source.
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
	type AutoPlayDeckSnap
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
			// Audio clock, NOT d.position_ms: that mirror is published from
			// requestAnimationFrame, which the browser stops in a background tab,
			// and a frozen position never reaches AUTO_PLAY_THRESHOLD_MS. Reading
			// the clock is what lets a set keep mixing while the user is on
			// another tab. This poll is a setInterval, which a tab playing audio
			// keeps running (throttled to ~1s), leaving ~16 chances inside the
			// 16s window.
			position_ms: deckAudioClockPositionMs(id),
			duration_ms: d.duration_ms,
			is_master: d.is_master,
			beat_sync_enabled: d.beat_sync_enabled
		};
	});
}

function _excludeIds(sourceId: DeckId, snaps: readonly AutoPlayDeckSnap[]): Set<string> {
	const out = new Set<string>();
	for (const d of snaps) {
		if (d.id === sourceId) continue;
		if (d.stable_id !== null) out.add(d.stable_id);
	}
	for (const id of _unplayableIds) out.add(id);
	return out;
}

function _syncPlayedSet(): void {
	const epoch = getAutoPlayFeedEpoch();
	if (epoch !== _playedFeedEpoch) {
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

async function _handoff(source: AutoPlayDeckSnap, follower: DeckId, nextId: string): Promise<void> {
	const occupied = deckStates[follower].stable_id;
	if (occupied !== null && occupied !== nextId) {
		await dispatchPerformanceCommand({ type: 'unload', deck: follower });
	}
	if (deckStates[follower].stable_id !== nextId) {
		await dispatchPerformanceCommand({ type: 'load', deck: follower, stable_id: nextId });
	}
	await _applyBeatSyncDecision(source, follower);
	await dispatchPerformanceCommand({ type: 'play', deck: follower, playing: true });
	// pickSourceDeck only ever arms off the master, so AutoPlay must move
	// master to the deck it just started - otherwise the next tick still
	// sees the (now trailing/finished) old master as source and stalls.
	await dispatchPerformanceCommand({ type: 'master', deck: follower });
	if (source.stable_id !== null) _playedIds.add(source.stable_id);
	_playedIds.add(nextId);
}

async function _tick(): Promise<void> {
	if (!uiPrefs.auto_play_enabled || _inFlight) {
		if (!uiPrefs.auto_play_enabled) _publishOrder([]);
		return;
	}

	const snaps = _snaps();
	const source = pickSourceDeck(snaps);
	if (source === null || source.stable_id === null) {
		_triggeredFor = null;
		_waitingFollowerFor = null;
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

	_inFlight = true;
	_triggeredFor = source.stable_id;
	try {
		await _handoff(source, follower, nextId);
		_attemptsFor = { source: '', count: 0 };
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
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
		_playedIds = new Set();
		_unplayableIds = new Set();
		_attemptsFor = { source: '', count: 0 };
		_playedFeedEpoch = -1;
		_publishOrder([]);
	};
}
