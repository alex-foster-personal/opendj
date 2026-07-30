/**
 * /performance auto-play controller (v1 hard-cut).
 *
 * When prefs.auto_play_enabled: as the master (or playing) deck enters the
 * remaining-time window, load the next suggested/playlist track onto a free
 * or stopped follower and start it via performance-ipc. Beat Sync is mirrored
 * from the source deck before play. Crossfade is deferred (GitHub follow-up).
 */
import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
import {
	AUTO_PLAY_THRESHOLD_MS,
	getAutoPlayPlaylistIds,
	getAutoPlaySuggestIds,
	pickFollowerDeck,
	pickNextStableId,
	pickSourceDeck,
	remainingMs,
	shouldTriggerAutoPlay,
	type AutoPlayDeckSnap
} from '$lib/rb/auto-play';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';
import type { DeckId } from '$lib/rb/types';

const POLL_MS = 250;

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

async function _handoff(source: AutoPlayDeckSnap, follower: DeckId, nextId: string): Promise<void> {
	const occupied = deckStates[follower].stable_id;
	if (occupied !== null && occupied !== nextId) {
		await dispatchPerformanceCommand({ type: 'unload', deck: follower });
	}
	if (deckStates[follower].stable_id !== nextId) {
		await dispatchPerformanceCommand({ type: 'load', deck: follower, stable_id: nextId });
	}
	if (source.beat_sync_enabled && !deckStates[follower].beat_sync_enabled) {
		await dispatchPerformanceCommand({ type: 'beat_sync', deck: follower, enabled: true });
	}
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
