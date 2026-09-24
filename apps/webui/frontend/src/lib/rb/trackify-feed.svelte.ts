/**
 * Reactive Trackify feed hydrator and skip-next latch (PERFMODE-15).
 */
import type { AutoPlayTrackRow } from '$lib/rb/auto-play-chain';
import {
	createTrackifyFeedController,
	fetchTrackifyViewRows,
	trackifyPlaylistScope,
	type TrackifyFeedSnapshot
} from '$lib/rb/trackify-feed';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/rb/performance-ipc.svelte';

let _controller = createTrackifyFeedController();
let _hydrating = false;
let _skipNext = false;
// Bumped by installTrackifyFeed() on both install and its own teardown, so a
// hydrate started by one session can never publish into (or clear the
// in-flight flag of) a session that has since torn down or been replaced
// (Sol review, PR #3676): teardown resets `_controller`, but neither
// cancelled nor invalidated the fetch itself, so a pending hydrate could
// resolve after unmount and write into the just-reset controller, or its
// `finally` could clear `_hydrating` out from under a brand-new session's
// own in-flight fetch.
let _installGeneration = 0;
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

async function _hydrate(ownGeneration: number): Promise<void> {
	if (_hydrating) return;
	_hydrating = true;
	// Captured before the try so `finally` can detect a scope change
	// regardless of whether the fetch below resolves or rejects (Sol review
	// round 12, PR #3676: a request that rejects for the OLD scope used to
	// leave `rehydrateForNewScope` unset, since that flag was only reachable
	// from the try's success path -- so a scope switch during a FAILING
	// fetch sat on the old snapshot until the 60 s interval, same as the
	// success-path bug round 8 already fixed for a resolved-but-stale
	// response).
	let requestedScope: string | null = null;
	try {
		// Captured ONCE, before the await: `uiPrefs.last_playlist` can change
		// while this fetch is in flight (the operator picks a different
		// playlist), and re-reading it afterward to publish would stamp rows
		// fetched for the OLD scope as belonging to whatever is selected NOW,
		// corrupting PLAY-04 snapshot semantics (Sol review, PR #3676). A
		// fetch that is no longer for the current scope by the time it
		// resolves is discarded outright rather than published under either
		// scope; the next hydrate cycle covers whatever is selected now.
		const lastPlaylist = uiPrefs.last_playlist;
		requestedScope = trackifyPlaylistScope(lastPlaylist);
		const viewRows = await fetchTrackifyViewRows(lastPlaylist);
		// Discard outright if this session has since torn down (or a new one
		// installed) while the fetch was in flight -- publishing here would
		// write into a controller a newer/absent session already reset.
		if (_installGeneration !== ownGeneration) return;
		if (trackifyPlaylistScope(uiPrefs.last_playlist) !== requestedScope) return;
		_lastSnapshot = _controller.step(true, lastPlaylist, viewRows);
	} finally {
		// Only this generation's own hydrate may clear the shared in-flight
		// flag: a stale hydrate's finally must not clobber a newer session's
		// own fetch that is genuinely still in flight.
		if (_installGeneration === ownGeneration) {
			_hydrating = false;
			// Checked here (not via a flag set only on the try's success
			// path) so it fires whether the fetch resolved for a scope the
			// operator has since left, OR rejected outright -- either way,
			// without this the feed sits empty or on the old snapshot for
			// up to 60 s, until the next interval tick (Sol review, PR #3676).
			if (requestedScope !== null && trackifyPlaylistScope(uiPrefs.last_playlist) !== requestedScope) {
				_hydrateOrToast(ownGeneration);
			}
		}
	}
}

/** Surfaces a failed hydration instead of leaving an unhandled rejection
 * with the feed silently stuck empty (PERFMODE-15 review finding). */
function _hydrateOrToast(ownGeneration: number): void {
	_hydrate(ownGeneration).catch((error: unknown) => {
		if (_installGeneration !== ownGeneration) return;
		const reason = error instanceof Error ? error.message : String(error);
		pushToast(`Trackify: could not load the feed (${reason})`, 'error');
	});
}

export function installTrackifyFeed(): () => void {
	_installGeneration += 1;
	const ownGeneration = _installGeneration;
	_hydrateOrToast(ownGeneration);
	const interval = setInterval(() => {
		_hydrateOrToast(ownGeneration);
	}, 60_000);
	return () => {
		clearInterval(interval);
		// Invalidates THIS session's own generation too, not just a future
		// one: a hydrate already in flight when teardown runs must never
		// publish into the controller reset below, even if nothing remounts.
		_installGeneration += 1;
		_controller = createTrackifyFeedController();
		_lastSnapshot = { scope: '', epoch: 0, rows: [], snapshotted: false };
		_skipNext = false;
		_hydrating = false;
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
