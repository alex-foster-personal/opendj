/**
 * Library health dot policy. Pure, and the ONLY place the browser panel's
 * "Library health" liveness dot text and state are decided.
 *
 * Split out the same way as `meter-math.ts`: `BrowserPanel.svelte` measures
 * (playlists loaded, the reconcile summary's counts, any reconcile error)
 * and decides nothing; every threshold and wording choice lives here, where
 * it can be unit tested without a component, an AudioContext, or a network
 * mock. Sol thread 3966631804 on PR #1560 flagged an earlier version of the
 * test that sliced this function's source out of the .svelte file and
 * re-evaluated it with `Function(...)` - that reconstructed function was
 * never the production code path. This module exists so the test can import
 * and call the exact function the component runs, with no slicing.
 *
 * pin a66ee132a14e asks for two things this module answers: the "playlist
 * library found" liveness signal, and a total that never quotes rows whose
 * files are gone - the count must say "non-broken", not "playable", because
 * `_scan_broken()` (apps/webui/server/routes/reconcile.py) excludes
 * streaming and pathless rows from `total_broken` while `_loadOntoDeck()`
 * still refuses to load them, so a naive "playable" label overclaims for
 * exactly those rows. the maintainer's own pin wording: "should always refer to
 * number of non-broken if quoting total".
 */

export type LibraryHealthDot = {
	label:
		| 'Frontend'
		| 'Backend'
		| 'Library health'
		| 'Vocals completion'
		| 'Stems completion'
		| 'Lyrics completion';
	state: 'loading' | 'complete' | 'incomplete' | 'unavailable' | 'error';
	detail: string;
};

export function libraryHealthDot(
	libraryHealthError: string | null,
	allTracksCount: number | null,
	playlistCount: number,
	allTracksNonBrokenCount: number | null,
	allTracksBrokenCount: number | null,
	allTracksReconcileError: string | null
): LibraryHealthDot {
	const label = 'Library health' as const;
	if (libraryHealthError !== null) {
		return { label, state: 'error', detail: libraryHealthError };
	}
	if (allTracksCount === null) {
		return { label, state: 'loading', detail: 'checking library health' };
	}
	const playlistPart =
		playlistCount > 0 ? `${playlistCount} ${playlistCount === 1 ? 'playlist' : 'playlists'} found` : 'no playlists found';
	// Until the reconcile summary lands there is no honest non-broken total,
	// so the dot says the count is still settling rather than quoting the
	// raw row count in the meantime - quoting it is the bug. But if the
	// reconcile summary FAILED (backend error, timeout, contract break),
	// allTracksNonBrokenCount never settles - it stays null forever - so
	// this branch must not be a permanent home for it. Surface the
	// reconcile error and go red instead of freezing on "counting
	// non-broken tracks" indefinitely.
	if (allTracksNonBrokenCount === null) {
		if (allTracksReconcileError !== null) {
			return { label, state: 'error', detail: allTracksReconcileError };
		}
		return {
			label,
			state: allTracksCount > 0 ? 'incomplete' : 'unavailable',
			// "counting" implies pending work, which is wrong when the
			// library is already known to be empty (allTracksCount === 0) -
			// there is nothing left to count.
			detail:
				allTracksCount > 0
					? `${playlistPart}, counting non-broken tracks`
					: `${playlistPart}, library empty`
		};
	}
	const broken = allTracksBrokenCount ?? 0;
	const brokenPart =
		broken > 0 ? `, ${broken} broken ${broken === 1 ? 'link' : 'links'}` : ', no broken links';
	if (allTracksNonBrokenCount <= 0) {
		return {
			label,
			state: 'unavailable',
			detail:
				broken > 0
					? `${playlistPart}, no non-broken tracks - all ${broken} ${broken === 1 ? 'link is' : 'links are'} broken`
					: `${playlistPart}, no non-broken tracks`
		};
	}
	return {
		label,
		state: broken > 0 ? 'incomplete' : 'complete',
		detail: `${playlistPart}, ${allTracksNonBrokenCount} non-broken${brokenPart}`
	};
}
