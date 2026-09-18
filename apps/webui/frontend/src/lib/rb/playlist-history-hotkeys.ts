// Cmd/Ctrl+Z undoes the last playlist edit; Cmd/Ctrl+Shift+Z redoes.
// Dispatches through the performance IPC so the history panel buttons and
// an agent over window.musicDjToolsPerformance share one path.
import { isSettingsOpen } from '$lib/settings/overlay.svelte';
import { isTextEntryTarget } from '$lib/keyboard/text-entry-target';
import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';

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
		} else {
			void runPerformanceCommandFromUi({ type: 'playlist_undo' });
		}
	};
	window.addEventListener('keydown', onKey);
	return () => window.removeEventListener('keydown', onKey);
}
