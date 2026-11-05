/** Shared playlist undo/redo + history list for library toolbar chrome, and the
 * other library-chrome commands that go through the agent command path. */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import {
	fetchPlaylistHistory,
	jumpPlaylistHistory,
	redoPlaylistEdit,
	undoPlaylistEdit,
	type PlaylistHistoryEntry,
	type PlaylistHistoryNavTarget
} from '$lib/rb/playlist-history';
import {
	applyHistoryStep,
	HISTORY_TOAST_GROUP,
	historyJumpToastMessage,
	type HistoryToastAction,
	type HistoryUndoRestorer
} from '$lib/rb/playlist-history-apply';
import { pushToast, TOAST_DEFAULT_MS } from '$lib/stores.svelte';
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

let _undoRestorer: HistoryUndoRestorer | null = null;

/** LIBUX-8676: the browser offers to put undone removals back in place. */
export function registerPlaylistUndoRestorer(restorer: HistoryUndoRestorer): () => void {
	_undoRestorer = restorer;
	return () => {
		if (_undoRestorer === restorer) _undoRestorer = null;
	};
}

function _historyToast(message: string, action?: HistoryToastAction): void {
	pushToast(message, 'info', TOAST_DEFAULT_MS, undefined, {}, HISTORY_TOAST_GROUP, action);
}

/** Mount once under LibraryNav; wires IPC adapter and event refresh. */
export function installPlaylistHistoryChrome(): () => void {
	// LIBUX-8676: every undo path (button, Cmd+Z, toast, agent bus) lands
	// here, so the toast and the in-place restore are raised once.
	const unregister = registerPlaylistHistoryAdapter({
		undo: async () => {
			const { entries, cursor } = playlistHistoryChrome;
			await applyHistoryStep({
				action: 'undo',
				expected: cursor > 0 ? entries[cursor - 1] : undefined,
				send: undoPlaylistEdit,
				restorer: _undoRestorer,
				toast: _historyToast,
				opposite: redoPlaylistFromChrome
			});
		},
		redo: async () => {
			const { entries, cursor } = playlistHistoryChrome;
			await applyHistoryStep({
				action: 'redo',
				expected: entries[cursor],
				send: redoPlaylistEdit,
				restorer: null,
				toast: _historyToast,
				opposite: undoPlaylistFromChrome
			});
		},
		jump: async (commandId) => {
			const message = historyJumpToastMessage(await jumpPlaylistHistory(commandId));
			if (message !== null) _historyToast(message);
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

/** When the edit the next playlist undo would reverse happened, or null. */
export function newestPlaylistEditAt(): number | null {
	const { entries, cursor, canUndo } = playlistHistoryChrome;
	const top = canUndo && cursor > 0 ? entries[cursor - 1] : undefined;
	return top === undefined ? null : Date.parse(top.ts);
}

export function undoPlaylistFromChrome(): void {
	void runPerformanceCommandFromUi({ type: 'playlist_undo' });
}

export function redoPlaylistFromChrome(): void {
	void runPerformanceCommandFromUi({ type: 'playlist_redo' });
}

/** LIBUX-8626: a history row click, through the same command agents send. */
export async function jumpPlaylistHistoryFromChrome(commandId: string): Promise<boolean> {
	const result = await runPerformanceCommandFromUi({ type: 'playlist_history_jump', command_id: commandId });
	return result.ok;
}

/** LIBUX-8626: show what a history row touched (its playlist, its track). */
export function showHistoryTargetFromChrome(target: PlaylistHistoryNavTarget): void {
	void runPerformanceCommandFromUi({ type: 'browser_select_playlist', ...target });
}

/** UX-RAIL-01: the icon rail's top-left entry. The same `browser_select_playlist`
 * command agents use, so the button and an agent agree on what "All Tracks" is. */
export function showWholeCollectionFromChrome(pressT0Ms?: number): void {
	void runPerformanceCommandFromUi({ type: 'browser_select_playlist', playlist_id: 'all' }, pressT0Ms);
}
