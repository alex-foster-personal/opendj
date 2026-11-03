<script lang="ts">
	import { onDestroy } from 'svelte';
	import type { SmartlistSummary } from '$lib/rb/api-smartlists';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import type TreeContextMenu from './TreeContextMenu.svelte';
	import TreeSmartlistSectionView from './TreeSmartlistSectionView.svelte';
	import { TreeSmartlistRename } from './tree-smartlist-rename.svelte';
	import { TreeSmartlists } from './tree-smartlists.svelte';

	let {
		selectedId,
		onselectsmartlist,
		treeContextMenu,
		onDeleteReady
	}: {
		selectedId: string | null;
		onselectsmartlist?: ((smartlist: SmartlistSummary) => void) | undefined;
		treeContextMenu: TreeContextMenu | null;
		onDeleteReady?: (fn: (sl: { id: string; name: string }) => void) => void;
	} = $props();

	const smartlists = new TreeSmartlists(() => onselectsmartlist);
	const rename = new TreeSmartlistRename(() => smartlists.rows, smartlists);
	onDestroy(() => smartlists.destroy());

	$effect(() => {
		rename.checkPending();
	});

	export async function createAndRename(): Promise<void> {
		await rename.createAndRename();
	}

	async function deleteSmartlistUi(sl: { id: string; name: string }): Promise<void> {
		const skip = uiPrefs.confirm.delete_playlist === false;
		if (!skip && !window.confirm(`Delete smartlist "${sl.name}"?`)) return;
		try {
			await smartlists.remove(sl.id);
			pushToast(`Deleted smartlist "${sl.name}"`, 'info');
		} catch (exc) {
			pushToast(`delete failed: ${String(exc)}`, 'error');
		}
	}

	async function duplicateSmartlistUi(sl: SmartlistSummary): Promise<void> {
		try {
			const copy = await smartlists.duplicate(sl.id);
			pushToast(`Duplicated as "${copy.name}"`, 'info');
			await rename.begin(copy);
		} catch (exc) {
			pushToast(`duplicate failed: ${String(exc)}`, 'error');
		}
	}

	export function beginRename(sl: { id: string; name: string }): void {
		const row = smartlists.rows?.find((r) => r.id === sl.id);
		if (row !== undefined) void rename.begin(row);
	}

	export function duplicateFromMenu(sl: { id: string; name: string }): void {
		const row = smartlists.rows?.find((r) => r.id === sl.id);
		if (row !== undefined) void duplicateSmartlistUi(row);
	}

	$effect(() => {
		onDeleteReady?.(deleteSmartlistUi);
	});
</script>

<TreeSmartlistSectionView
	open={smartlists.open}
	rows={smartlists.rows}
	error={smartlists.error}
	{selectedId}
	selectable={!!onselectsmartlist}
	editingId={rename.editingId}
	editDraft={rename.editDraft}
	bind:renameInputEl={rename.inputEl}
	ontoggle={() => smartlists.toggle()}
	onclickrow={(sl) => smartlists.click(sl)}
	onmenukey={(e, sl) => treeContextMenu?.openSmartlistFromKeyboard(e, sl)}
	oncontextmenurow={(e, sl) => treeContextMenu?.openSmartlist(e, sl)}
	oneditdraft={(value) => (rename.editDraft = value)}
	onrenamekeydown={(e) => rename.onKeydown(e)}
	onrenamecommit={() => void rename.commit()}
/>
