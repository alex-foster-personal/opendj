/**
 * Shared-graph entry for the Trackify feed hydrate tests
 * (trackify-feed-hydrate.test.mjs, PERFMODE-15).
 *
 * `uiPrefs` and the feed module must come from the SAME esbuild bundle so a
 * test's writes to `uiPrefs.last_playlist` are visible to
 * `trackify-feed.svelte.ts`'s own import of it -- two separate
 * `loadTypeScriptModule` calls would each bundle (and initialize) an
 * independent copy of `prefs.svelte.ts`.
 */
export { uiPrefs } from '$lib/rb/prefs.svelte';
export {
	e2ePrimeTrackifyFeed,
	getTrackifyFeedEpoch,
	getTrackifyFeedRows,
	installTrackifyFeed,
	readTrackifyFeedSnapshot,
	readTrackifySkipNext,
	noteTrackifySkipNext
} from '$lib/rb/trackify-feed.svelte';
