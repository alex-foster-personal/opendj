export { plannedTitle } from '$lib/rb/planned-explainers';
// Through here, not imported directly, to keep BrowserPanel's import fan-out
// (frontend.max_fan_out quality ratchet) at main's figure.
export { libraryEditShortcut } from './library-edit-shortcut';
// The clipboard rules load on the first shortcut press, outside the
// /performance static bundle (scripts/bundle-budget.mjs).
export const loadTrackClipboard = () => import('./track-clipboard');
// The delete/drop confirm dialog loads on first use, for the same reason.
export const loadBrowserConfirmDialog = () => import('./BrowserConfirmDialog.svelte');
export { enqueueLibraryJobsBatched } from '$lib/rb/api-library-jobs';
export { libraryJobsStore } from '$lib/rb/library-jobs-store.svelte';
export { default as LibraryJobsChrome } from '../library-jobs/LibraryJobsChrome.svelte';
export { anyDeckPlaying, createPlayingGate } from '$lib/rb/playing-gate';
export {
	resolveRowMarkerAnlz,
	resolveRowPreviewStrip,
	resolveRowStripLoading,
	resolveRowVocals
} from '$lib/rb/row-vocals';
export {
	computeNextOnlyRef,
	isAppropriateNext,
	resolveSearchFilterFallback,
	selectSearchFilterFallback,
	type NextOnlyRef
} from '$lib/rb/next-only-filter';
export { getIngestCoverage, type IngestCoverage } from '$lib/rb/api-ingest';
export { libraryHealthDot, type LibraryHealthDot } from '$lib/rb/library-health-dots';
export { completeLibraryUsable, recordOpenToLibraryRows } from '$lib/client-telemetry';
export { formatReplaceStateUrl } from '$lib/rb/performance-deeplink';
export { addToPlaylistToastMessage, appendTracksToPlaylist } from '$lib/rb/add-to-playlist';
export { removeFromLibrary } from '$lib/rb/track-library';
export {
	isCurrentBrowserSearch,
	reportBrowserSearchResult,
	subscribeBrowserSearch,
	type BrowserSearchRequest
} from '$lib/rb/browser-search';
export {
	isLibraryPanelsCollapsed,
	noteVisibleLibraryRowCount,
	setLibraryPanelsCollapsed,
	toggleLibraryPanels
} from '$lib/rb/library-panels.svelte';
export {
	createAutoPlayFeedSnapshot,
	getAutoPlayRankOf,
	setAutoPlayTrackFeed
} from '$lib/rb/auto-play';
export { getSpotifyPendingTracks, type SpotifyPendingTrack } from '$lib/rb/spotify-api';
export { fillAllTracksFromIndex, fillAllTracksPane, rowsForIndex } from './fill-all-tracks';
export { clearPlaylistRowCache, fillPlaylistPane } from './fill-playlist-pane';
export { fillAutolistPane } from './fill-autolist';
export {
	autolistNode,
	isAutolistId,
	AUTOLIST_ID
} from './autolist-ids';
export { queryAutolists } from '$lib/rb/api-autolists';
export {
	emptyAutolistSelection,
	hasAutolistSelection,
	type AutolistSelection
} from '$lib/smartlists/autolist-rule';
export { ensureAudioPrefetch } from '$lib/rb/audio-prefetch-cache.svelte';
export { clearSelection, pruneSelection } from './pane-row-selection';
export { fetchAllPages } from './virtual-window';
export { libraryAudioLoadRefusal, rowFromListWire, rowFromPlaylistWire } from './browser-row-wire';
export { applySettledAvailability } from './browser-row-wire';
export { startPendingSettle } from './pending-availability-settle';
export { default as PlaylistSetTabs } from './PlaylistSetTabs.svelte';
export { default as CompatibleFilterPopover } from './CompatibleFilterPopover.svelte';
export { setTabLabel } from './playlist-set-tabs';
/** A stick track row whose stick was pulled (USBPLAY-09: the browse store
 * grays it to awaiting_volume), refused as "Stick removed" (spec 4b) rather
 * than as a broken link. Stick ids start `usb-`; library ids are sha1 hex, so
 * the prefix cannot match one (the same test as track-source's isUsbTrackId). */
export function isRemovedStickRow(row: {
	stable_id: string;
	file_availability?: string | null;
}): boolean {
	return row.file_availability === 'awaiting_volume' && row.stable_id.startsWith('usb-');
}

/** Play from USB pane source (USBPLAY-05): dynamic, so the stick store and
 * row mapping load on the first stick pane, not with /performance. */
export function usbPaneSource(): Promise<typeof import('$lib/rb/usb-library.svelte')> {
	return import('$lib/rb/usb-library.svelte');
}

// Re-exported so BrowserPanel.svelte, already coupled to this barrel, does not
// take more direct fan-out edges for IOPIN-01 keyboard nav, the Cmd+A/C/X/V
// library edit keys and the MIDI browse adapter.
export { registerBrowseAdapter } from '$lib/rb/midi/browse-adapter';
export { createBrowserKeyboard } from './browser-keyboard';
export { createLibraryEditKeys } from './library-edit-keys';
export { openIoView } from '$lib/rb/io-surface.svelte';
export { PairingIndex } from '$lib/rb/pairing-index.svelte';
// Through here, not imported directly, to keep BrowserPanel's import fan-out
// (frontend.max_fan_out quality ratchet) at main's figure.
export {
	fetchPlaylistFirstPage,
	prefetchPlaylistFirstPage,
	prefetchPlaylistTreeIntent,
	invalidatePlaylistFirstPage,
	invalidateAllPlaylistFirstPages
} from '$lib/rb/library-playlist-page-prefetch';
// The order answers when a playlist selection lands, not when its fill ends.
export { paneLoadState, untilSelectionLands } from './selection-lands';
