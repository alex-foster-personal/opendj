import type { ContextMenuItem } from '../ContextMenu.svelte';
import { bindMenuItems } from '$lib/context-menu/bind';
import { loadMenuRegistry, menuRegistry } from '$lib/context-menu/lazy.svelte';
import { reportMenuLoadFailure } from './track-context-menu';
import { startIngestRefresh } from '$lib/rb/api-ingest';
import { openSettings } from '$lib/settings/overlay.svelte';
import { pushToast } from '$lib/stores.svelte';

async function refreshLibrary(): Promise<void> {
	try {
		await startIngestRefresh();
		pushToast('Library refresh started', 'info');
	} catch (err) {
		pushToast(err instanceof Error ? err.message : String(err), 'error');
	}
}

/** Load the registry and return library-source menu items, or null when the chunk never arrives. */
export async function librarySourceMenuItems(): Promise<ContextMenuItem[] | null> {
	loadMenuRegistry(reportMenuLoadFailure);
	for (let attempt = 0; attempt < 200; attempt++) {
		const reg = menuRegistry();
		if (reg !== null) {
			return bindMenuItems(reg.menuFor({ kind: 'library-source' }, {}), {
				'source-refresh': () => void refreshLibrary(),
				'source-settings': () => openSettings('folder watch rescan')
			});
		}
		await new Promise<void>((resolve) => setTimeout(resolve, 16));
	}
	return null;
}
