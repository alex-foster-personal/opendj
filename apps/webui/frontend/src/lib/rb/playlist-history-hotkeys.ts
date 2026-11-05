// Cmd/Ctrl+Z undoes the newest library change, a playlist edit or a tag change;
// Cmd/Ctrl+Shift+Z redoes playlist edits.
// Dispatches through the performance IPC so the history panel buttons and
// an agent over window.musicDjToolsPerformance share one path.
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { newestTagWriteAt } from '$lib/rb/api-edit-suite';
import { undoNewestTagChange } from '$lib/rb/library-tags.svelte';
import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
import { newestPlaylistEditAt } from '$lib/rb/playlist-history-chrome.svelte';
import { undoTarget } from '$lib/rb/tag-display';

export function installPlaylistHistoryHotkeys(): () => void {
	const onKey = (e: KeyboardEvent): void => {
		if (e.repeat) return;
		if (isSettingsOpen()) return;
		if (isTextEntryTarget(e.target)) return;
		if (!(e.metaKey || e.ctrlKey)) return;
		if (e.key !== 'z' && e.key !== 'Z') return;
		e.preventDefault();
		if (e.shiftKey) {
			void runPerformanceCommandFromUi({ type: 'playlist_redo' });
		} else if (undoTarget(newestTagWriteAt(), newestPlaylistEditAt()) === 'tag') {
			void undoNewestTagChange();
		} else {
			void runPerformanceCommandFromUi({ type: 'playlist_undo' });
		}
	};
	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}
