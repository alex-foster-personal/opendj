/**
 * Trackify route session installer: engine lifecycle + feed + autoplay + IPC.
 */
import { engine } from '$lib/rb/audio-engine.svelte';
import { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';
import { setAppMode, setAutoPlayEnabled } from '$lib/rb/prefs.svelte';
import { installTrackifyAutoplay } from '$lib/rb/trackify-autoplay.svelte';
import { installTrackifyFeed } from '$lib/rb/trackify-feed.svelte';
import { installTrackifyBrowserIpc } from '$lib/rb/trackify-ipc.svelte';
import { dispatchPerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import { TRACKIFY_DECK_ID } from '$lib/rb/trackify-autoplay';

export function installTrackifySession(): () => void {
	setAppMode('music-player');
	setAutoPlayEnabled(true);
	const uninstallFeed = installTrackifyFeed();
	const uninstallAutoplay = installTrackifyAutoplay();
	const uninstallPerfIpc = installPerformanceBrowserIpc();
	const uninstallTrackifyIpc = installTrackifyBrowserIpc();
	return () => {
		uninstallTrackifyIpc();
		uninstallPerfIpc();
		uninstallAutoplay();
		uninstallFeed();
		void dispatchPerformanceCommand({ type: 'play', deck: TRACKIFY_DECK_ID, playing: false }).catch(
			() => undefined
		);
		void engine.dispose();
	};
}
