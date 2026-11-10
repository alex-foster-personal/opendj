/**
 * Hostile-empty AutoPlay feed fixtures: default to BOOT (no rows) until a test
 * hydrates explicitly. Hides the mount race where auto_play_enabled precedes
 * the first playlist fetch (PLAY-06, issue #11215 / #700).
 */

/** One key and a tight BPM spread so ordering assertions isolate membership, not harmony. */
export const SAMPLE_ROWS = Object.freeze({
	byTitle: [
		{ stable_id: 'alpha', key: '8A', bpm: 124, file_exists: true },
		{ stable_id: 'bravo', key: '8A', bpm: 125, file_exists: true },
		{ stable_id: 'charlie', key: '8A', bpm: 126, file_exists: true }
	],
	byBpm: [
		{ stable_id: 'charlie', key: '8A', bpm: 126, file_exists: true },
		{ stable_id: 'bravo', key: '8A', bpm: 125, file_exists: true },
		{ stable_id: 'alpha', key: '8A', bpm: 124, file_exists: true }
	]
});

/**
 * @param {unknown} autoPlay module from src/lib/rb/auto-play.ts
 * @param {readonly object[]} [initialView]
 * @param {string} [initialScope]
 */
export function createHostileEmptyHarness(autoPlay, initialView = [], initialScope = 'playlist-a') {
	/** @type {AutoPlayFeedSnapshot} */
	const snapshot = autoPlay.createAutoPlayFeedSnapshot();
	let enabled = false;
	let view = initialView;
	let scope = initialScope;
	let membershipHydrating = false;
	const apply = () => {
		const decision = snapshot.step(enabled, scope, view, '', membershipHydrating);
		if (decision.publish !== null) autoPlay.setAutoPlayTrackFeed(scope, decision.publish);
		return decision;
	};
	return {
		snapshot,
		get view() {
			return view;
		},
		setMembershipHydrating(next) {
			membershipHydrating = next;
		},
		sort(next) {
			view = next;
			return apply();
		},
		switchPlaylist(nextScope, nextView) {
			scope = nextScope;
			view = nextView;
			return apply();
		},
		setEnabled(next) {
			enabled = next;
			return apply();
		},
		filter(next, filterKey) {
			view = next;
			const decision = snapshot.step(enabled, scope, view, filterKey, membershipHydrating);
			if (decision.publish !== null) autoPlay.setAutoPlayTrackFeed(scope, decision.publish);
			return decision;
		},
		apply
	};
}
