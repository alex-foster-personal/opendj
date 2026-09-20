/**
 * Reactive Trackify feed hydrator and skip-next latch (PERFMODE-15).
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import {
	createTrackifyFeedController,
	fetchTrackifyViewRows,
	type TrackifyFeedSnapshot
} from '$lib/rb/trackify-feed';
import { uiPrefs } from '$lib/rb/prefs.svelte';

let _controller = createTrackifyFeedController();
let _hydrating = false;
let _skipNext = false;
let _lastSnapshot: TrackifyFeedSnapshot = {
	scope: '',
	epoch: 0,
	rows: [],
	snapshotted: false
};

export function getTrackifyFeedRows(): readonly AutoPlayTrackRow[] {
	return _controller.rows;
}

export function getTrackifyFeedEpoch(): number {
	return _controller.epoch;
}

export function readTrackifySkipNext(): boolean {
	if (!_skipNext) return false;
	_skipNext = false;
	return true;
}

export function noteTrackifySkipNext(): void {
	_skipNext = true;
}

async function _hydrate(): Promise<void> {
	if (_hydrating) return;
	_hydrating = true;
	try {
		const viewRows = await fetchTrackifyViewRows(uiPrefs.last_playlist);
		_lastSnapshot = _controller.step(true, uiPrefs.last_playlist, viewRows);
	} finally {
		_hydrating = false;
	}
}

export function installTrackifyFeed(): () => void {
	void _hydrate();
	const interval = setInterval(() => {
		void _hydrate();
	}, 60_000);
	return () => {
		clearInterval(interval);
		_controller = createTrackifyFeedController();
		_lastSnapshot = { scope: '', epoch: 0, rows: [], snapshotted: false };
		_skipNext = false;
	};
}

export function readTrackifyFeedSnapshot(): TrackifyFeedSnapshot {
	return _lastSnapshot;
}

/** Dev/e2e only: prime the PLAY-04 feed with explicit rows (PERFMODE-15 acceptance). */
export function e2ePrimeTrackifyFeed(rows: readonly AutoPlayTrackRow[]): void {
	if (!import.meta.env.DEV) {
		throw new Error('e2ePrimeTrackifyFeed is only available in dev builds');
	}
	_lastSnapshot = _controller.step(true, uiPrefs.last_playlist, rows);
}
