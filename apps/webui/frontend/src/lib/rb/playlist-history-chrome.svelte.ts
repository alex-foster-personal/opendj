/** Shared playlist undo/redo + history list for library toolbar chrome, and the
 * other library-chrome commands that go through the agent command path. */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import {
	fetchPlaylistHistory,
	redoPlaylistEdit,
	undoPlaylistEdit,
	type PlaylistHistoryEntry
} from '$lib/rb/playlist-history';
import {
	registerPlaylistHistoryAdapter,
	runPerformanceCommandFromUi
} from '$lib/rb/performance-ipc.svelte';

export const playlistHistoryChrome = $state({
	cursor: 0,
	canUndo: false,
	canRedo: false,
	entries: [] as PlaylistHistoryEntry[]
});

export async function refreshPlaylistHistoryChrome(): Promise<void> {
	const hist = await fetchPlaylistHistory();
	playlistHistoryChrome.cursor = hist.cursor;
	playlistHistoryChrome.canUndo = hist.can_undo;
	playlistHistoryChrome.canRedo = hist.can_redo;
	playlistHistoryChrome.entries = hist.entries;
}

/** Mount once under LibraryNav; wires IPC adapter and event refresh. */
export function installPlaylistHistoryChrome(): () => void {
	const unregister = registerPlaylistHistoryAdapter({
		undo: async () => {
			await undoPlaylistEdit();
		},
		redo: async () => {
			await redoPlaylistEdit();
		}
	});
	const unkind = subscribeKind('playlists', () => {
		void refreshPlaylistHistoryChrome();
	});
	const unresync = subscribeResync(() => {
		void refreshPlaylistHistoryChrome();
	});
	void refreshPlaylistHistoryChrome();
	return () => {
		unregister();
		unkind();
		unresync();
	};
}

export function undoPlaylistFromChrome(): void {
	void runPerformanceCommandFromUi({ type: 'playlist_undo' });
}

export function redoPlaylistFromChrome(): void {
	void runPerformanceCommandFromUi({ type: 'playlist_redo' });
}

/** UX-RAIL-01: the icon rail's top-left entry. The same `browser_select_playlist`
 * command agents use, so the button and an agent agree on what "All Tracks" is. */
export function showWholeCollectionFromChrome(pressT0Ms?: number): void {
	void runPerformanceCommandFromUi({ type: 'browser_select_playlist', playlist_id: 'all' }, pressT0Ms);
}
