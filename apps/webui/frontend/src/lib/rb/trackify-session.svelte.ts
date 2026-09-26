/**
 * Trackify route session installer: engine lifecycle + feed + autoplay + IPC.
 */
import { engine } from '$lib/rb/audio-engine.svelte';
import { currentGigRuntimeGeneration, noteGigRuntimeMounted } from '$lib/rb/library-mode-runtime';
import { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';
import { setAppMode, setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';
import { installTrackifyAutoplay } from '$lib/rb/trackify-autoplay.svelte';
import { blockStemDecode } from '$lib/rb/stem-decode-policy';
import { installTrackifyFeed } from '$lib/rb/trackify-feed.svelte';
import { installTrackifyBrowserIpc } from '$lib/rb/trackify-ipc.svelte';
import { dispatchPerformanceCommand, pushToast } from '$lib/rb/performance-ipc.svelte';
import { TRACKIFY_DECK_ID } from '$lib/rb/trackify-autoplay';

export function installTrackifySession(): () => Promise<void> {
	// Navigating straight from /performance (Gig) to /music-player fires
	// Gig's own teardown (releaseGigRuntime, PERFMODE-14) fire-and-forget on
	// unmount -- the SvelteKit route transition never awaits it. That release
	// yields mid-flight (disposing stem pools) before it calls
	// engine.dispose() on the SAME singleton this session is about to start
	// using, and only a generation bump changing since it started aborts it.
	// noteGigRuntimeMounted() is exactly that signal Gig's own remount uses
	// to supersede a stale release; sending it here makes a Trackify mount
	// supersede one too, so the stale Gig teardown retires before disposing
	// an engine Trackify has since claimed (Codex review, PR #3676).
	const ownEngineGeneration = noteGigRuntimeMounted();
	const priorAutoPlayEnabled = uiPrefs.auto_play_enabled;
	// PERFMODE-15: Trackify has no stems. Blocked for the whole session, at
	// every stem entry point the engine has, not per load.
	const releaseStemDecode = blockStemDecode('Trackify mode has no stems (PERFMODE-15)');
	setAppMode('music-player');
	setAutoPlayEnabled(true);
	const uninstallFeed = installTrackifyFeed();
	const uninstallAutoplay = installTrackifyAutoplay();
	const uninstallPerfIpc = installPerformanceBrowserIpc();
	const uninstallTrackifyIpc = installTrackifyBrowserIpc();
	return async () => {
		// First and synchronous: Svelte does not await this cleanup, so the next
		// route (Gig) can mount before the awaits below settle, and it must
		// already have its stems back.
		releaseStemDecode();
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
		// Svelte does not await an async onMount cleanup, so navigating
		// straight to Gig can mount and claim the shared engine WHILE this
		// teardown was suspended on the stop dispatch above. Recheck ownership
		// immediately before disposing: a generation bump here means a newer
		// session (Gig, or a remounted Trackify) has since claimed the engine,
		// and disposing now would tear IT down instead (Sol review, PR #3676;
		// symmetric with releaseGigRuntime's own post-await recheck for the
		// opposite Gig-to-Trackify direction).
		if (currentGigRuntimeGeneration() !== ownEngineGeneration) return;
		await engine.dispose();
	};
}
