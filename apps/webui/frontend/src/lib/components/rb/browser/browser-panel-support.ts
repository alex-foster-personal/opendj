export { plannedTitle } from '$lib/rb/planned-explainers';
// Through here, not imported directly, to keep BrowserPanel's import fan-out
// (frontend.max_fan_out quality ratchet) at main's figure.
export {
	clipboardToastMessage,
	getTrackClipboard,
	libraryEditShortcut,
	partitionPaste,
	pastedRowOrders,
	pasteRevealScrollTop,
	pasteBlockReason,
	pasteToastMessage,
	selectAllRows,
	selectedIdsInViewOrder,
	selectRowOrders,
	setTrackClipboard
} from './track-clipboard';
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
export { fillAllTracksPane } from './fill-all-tracks';
export { fillPlaylistPane } from './fill-playlist-pane';
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
export { rowFromListWire, rowFromPlaylistWire } from './browser-row-wire';
export { default as PlaylistSetTabs } from './PlaylistSetTabs.svelte';
export { setTabLabel } from './playlist-set-tabs';
