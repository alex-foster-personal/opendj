<script lang="ts">
	import { onDestroy } from 'svelte';
	import { pushToast } from '$lib/stores.svelte';
	import RecentlyDeletedFolderView from './RecentlyDeletedFolderView.svelte';
	import { TreeRecentlyDeleted } from './tree-recently-deleted.svelte';

	const recentlyDeleted = new TreeRecentlyDeleted();
	onDestroy(() => recentlyDeleted.destroy());

	async function restorePlaylist(id: string, name: string): Promise<void> {
		try {
			await recentlyDeleted.restore(id);
			pushToast(`Restored playlist "${name}"`, 'info');
		} catch (exc) {
			pushToast(`restore failed: ${String(exc)}`, 'error');
		}
	}
</script>

<RecentlyDeletedFolderView
	open={recentlyDeleted.open}
	rows={recentlyDeleted.rows}
	error={recentlyDeleted.error}
	ontoggle={() => recentlyDeleted.toggle()}
	onrestore={(id, name) => void restorePlaylist(id, name)}
/>
