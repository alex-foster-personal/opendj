/**
 * Trackify unsupervised single-deck autoplay controller (PERFMODE-15).
 */
import { deckAudioClockPositionMs, deckStates, pitchRanges } from '$lib/rb/audio-engine.svelte';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';
import {
	pickNextTrackifyCandidate,
	shouldAdvanceTrackify,
	TRACKIFY_DECK_ID,
	trackifyRemainingCandidates,
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
	const bounds = { min: 0.84, max: 1.16 };
	const pitch = pitchRanges[TRACKIFY_DECK_ID];
	if (Number.isFinite(pitch)) {
		const range = pitch / 100;
		bounds.min = Math.max(0.01, 1 - range);
		bounds.max = 1 + range;
	}
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

async function _loadAndPlay(nextId: string): Promise<void> {
	const deck = TRACKIFY_DECK_ID;
	try {
		if (deckStates[deck].stable_id !== null && deckStates[deck].stable_id !== nextId) {
			await dispatchPerformanceCommand({ type: 'unload', deck });
		}
		if (deckStates[deck].stable_id !== nextId) {
			await dispatchPerformanceCommand({ type: 'load', deck, stable_id: nextId });
		}
		await dispatchPerformanceCommand({ type: 'play', deck, playing: true });
		_queueHead = nextId;
	} catch (error: unknown) {
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
		_triggeredFor = null;
		_queueHead = null;
		return;
	}
	await _loadAndPlay(nextId);
}

async function _tick(): Promise<void> {
	if (!uiPrefs.auto_play_enabled) return;
	_syncEpoch();
	if (readTrackifySkipNext()) {
		_inFlight = true;
		try {
			await _advance('skip');
		} finally {
			_inFlight = false;
		}
		return;
	}
	if (_inFlight) return;
	const deck = _deckSnap();
	if (deck.stable_id === null) {
		const feed = getTrackifyFeedRows();
		if (feed.length === 0) return;
		_inFlight = true;
		try {
			const first = _pickNext(feed, deck);
			if (first !== null) await _loadAndPlay(first);
		} finally {
			_inFlight = false;
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
			_inFlight = false;
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
	};
}
