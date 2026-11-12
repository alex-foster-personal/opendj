import type BrowserPanel from '$lib/components/rb/BrowserPanel.svelte';

/** Shared browser panel for the root-mounted Cmd K palette (#9467). */
export const cmdkBrowser = $state<{ panel: BrowserPanel | null; hostMounted: boolean }>({
	panel: null,
	hostMounted: false
});

/** Publish the performance route's BrowserPanel while it is mounted. */
export function publishCmdkBrowser(panel: BrowserPanel): () => void {
	cmdkBrowser.panel = panel;
	return () => {
		if (cmdkBrowser.panel === panel) cmdkBrowser.panel = null;
	};
}
