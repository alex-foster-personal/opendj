/**
 * Trackify route session installer: engine lifecycle + feed + autoplay + IPC.
 */
import { engine } from '$lib/rb/audio-engine.svelte';
import { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';
import { setAppMode, setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';
import { installTrackifyAutoplay } from '$lib/rb/trackify-autoplay.svelte';
import { installTrackifyFeed } from '$lib/rb/trackify-feed.svelte';
import { installTrackifyBrowserIpc } from '$lib/rb/trackify-ipc.svelte';
import { dispatchPerformanceCommand, pushToast } from '$lib/rb/performance-ipc.svelte';
import { TRACKIFY_DECK_ID } from '$lib/rb/trackify-autoplay';

export function installTrackifySession(): () => Promise<void> {
	const priorAutoPlayEnabled = uiPrefs.auto_play_enabled;
	setAppMode('music-player');
	setAutoPlayEnabled(true);
	const uninstallFeed = installTrackifyFeed();
	const uninstallAutoplay = installTrackifyAutoplay();
	const uninstallPerfIpc = installPerformanceBrowserIpc();
	const uninstallTrackifyIpc = installTrackifyBrowserIpc();
	return async () => {
		uninstallTrackifyIpc();
		uninstallPerfIpc();
		uninstallAutoplay();
		uninstallFeed();
		setAutoPlayEnabled(priorAutoPlayEnabled);
		// A discarded stop-command rejection left the stop-then-dispose order
		// unguaranteed and any failure silent (Sol review, PR #3676): await it,
		// surface a real failure as a toast, and only then dispose the engine.
		try {
			await dispatchPerformanceCommand({ type: 'play', deck: TRACKIFY_DECK_ID, playing: false });
		} catch (error) {
			const reason = error instanceof Error ? error.message : String(error);
			pushToast(`Trackify: could not stop playback cleanly before teardown (${reason})`, 'error');
		}
		await engine.dispose();
	};
}
