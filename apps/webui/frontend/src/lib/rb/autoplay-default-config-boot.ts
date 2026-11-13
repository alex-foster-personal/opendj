/**
 * Default-config AutoPlay mount replay for unit tests (issue #11216, parent #700).
 * Replays BrowserPanel's feed steps without mounting Svelte.
 */
import {
	stepAutoPlayBrowserFeed,
	type AutoPlayBrowserRowLike
} from '$lib/components/rb/browser/autoplay-browser-feed';
import { setAutoPlayTrackFeed } from '$lib/rb/auto-play';
import { createAutoPlayFeedSnapshot, type AutoPlayFeedDecision } from '$lib/rb/autoplay-feed';
import {
	readAutoPlayDefaultConfigBootPrefs,
	type AutoPlayDefaultConfigMountPrefs
} from '$lib/rb/prefs.svelte';

export type AutoPlayDefaultConfigMountReplay = {
	readonly atMount: AutoPlayFeedDecision;
	readonly hydrated: AutoPlayFeedDecision;
	readonly prefs: AutoPlayDefaultConfigMountPrefs;
	readonly feedActiveAfterHydrate: boolean;
};

export function replayAutoPlayDefaultConfigMount(
	playlistId: string,
	rowsAtMount: readonly AutoPlayBrowserRowLike[],
	rowsAfterHydrate: readonly AutoPlayBrowserRowLike[],
	prefs: AutoPlayDefaultConfigMountPrefs = readAutoPlayDefaultConfigBootPrefs()
): AutoPlayDefaultConfigMountReplay {
	const feed = createAutoPlayFeedSnapshot();
	const atMount = stepAutoPlayBrowserFeed(feed, {
		enabled: prefs.auto_play_enabled,
		playlistId,
		renderedRows: rowsAtMount
	});
	if (atMount.publish !== null) {
		setAutoPlayTrackFeed(playlistId, atMount.publish);
	}
	const hydrated = stepAutoPlayBrowserFeed(feed, {
		enabled: prefs.auto_play_enabled,
		playlistId,
		renderedRows: rowsAfterHydrate
	});
	if (hydrated.publish !== null) {
		setAutoPlayTrackFeed(playlistId, hydrated.publish);
	}
	return {
		atMount,
		hydrated,
		prefs,
		feedActiveAfterHydrate: feed.active
	};
}
