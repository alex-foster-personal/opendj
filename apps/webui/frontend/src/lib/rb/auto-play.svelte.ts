/**
 * /performance auto-play controller (v1 hard-cut).
 *
 * When prefs.auto_play_enabled: as the master (or playing) deck enters the
 * remaining-time window, load the next playlist track onto a free or stopped
 * follower and start it via performance-ipc. Default pick: earliest un-played
 * membership row with Camelot key +-1 and BPM inside Beat Sync pitch bounds.
 * Optional enforce_play_order walks strict playlist order after current.
 * Beat Sync is requested only when a real BAR/BEAT phase-lock plan succeeds;
 * otherwise follower Beat Sync is explicitly disabled and play continues
 * free-tempo (see .planning/beat-sync-phase-lock-explainer-SA.md).
 */
import { DECK_IDS, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
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

let _timer: ReturnType<typeof setInterval> | null = null;
let _inFlight = false;
let _triggeredFor: string | null = null;
let _playedIds = new Set<string>();
let _playedFeedEpoch = -1;

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
	const out = new Set<string>();
	for (const d of snaps) {
		if (d.id === sourceId) continue;
		if (d.stable_id !== null) out.add(d.stable_id);
	}
	return out;
}

function _syncPlayedSet(): void {
	const epoch = getAutoPlayFeedEpoch();
	if (epoch !== _playedFeedEpoch) {
		_playedIds = new Set();
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
	if (source.stable_id !== null) _playedIds.add(source.stable_id);
	_playedIds.add(nextId);
}

async function _tick(): Promise<void> {
	if (!uiPrefs.auto_play_enabled || _inFlight) return;

	const snaps = _snaps();
	const source = pickSourceDeck(snaps);
	if (source === null || source.stable_id === null) {
		_triggeredFor = null;
		return;
	}

	const rem = remainingMs(source.position_ms, source.duration_ms);
	if (rem !== null && rem > AUTO_PLAY_THRESHOLD_MS) {
		// Seek back out of the window: allow a later re-arm for same track.
		if (_triggeredFor === source.stable_id) _triggeredFor = null;
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
		pushToast('auto-play: no free/stopped follower deck', 'error');
		_triggeredFor = source.stable_id;
		return;
	}

	_syncPlayedSet();
	const bounds = tempoBoundsFromPitchRange(pitchRanges[follower]);
	const sourceDeck = deckStates[source.id];
	const nextId = pickNextStableId({
		playlist: getAutoPlayPlaylist(),
		current_stable_id: source.stable_id,
		current_key: sourceDeck.key,
		current_bpm: sourceDeck.bpm,
		exclude_ids: _excludeIds(source.id, snaps),
		played_ids: _playedIds,
		enforce_play_order: uiPrefs.auto_play_enforce_order,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max
	});
	if (nextId === null) {
		pushToast(
			uiPrefs.auto_play_enforce_order
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
	} catch (error: unknown) {
		const message = error instanceof Error ? error.message : String(error);
		pushToast(`auto-play failed: ${message}`, 'error');
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
		_playedIds = new Set();
		_playedFeedEpoch = -1;
	};
}
