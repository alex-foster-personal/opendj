/**
 * /performance auto-play controller (v1 hard-cut).
 *
 * When prefs.auto_play_enabled: as the master (or playing) deck enters the
 * remaining-time window, load the next suggested/playlist track onto a free
 * or stopped follower and start it via performance-ipc. Beat Sync is requested
 * only when a real BAR/BEAT phase-lock plan succeeds against the source;
 * otherwise follower Beat Sync is explicitly disabled and play continues
 * free-tempo (see .planning/autoplay-beat-sync-phase-lock-SA.md). Crossfade
 * is deferred (GitHub follow-up).
 */
import { DECK_IDS, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import {
	AUTO_PLAY_THRESHOLD_MS,
	decideAutoPlayBeatSync,
	formatAutoPlaySyncSkipToast,
	getAutoPlayPlaylistIds,
	getAutoPlaySuggestIds,
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
			'error'
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

	const nextId = pickNextStableId({
		suggest_ids: getAutoPlaySuggestIds(),
		playlist_ids: getAutoPlayPlaylistIds(),
		current_stable_id: source.stable_id,
		exclude_ids: _excludeIds(source.id, snaps)
	});
	if (nextId === null) {
		pushToast('auto-play: no next track (suggest-next / playlist)', 'error');
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
	};
}
