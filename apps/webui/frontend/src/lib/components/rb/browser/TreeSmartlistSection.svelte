<script lang="ts">
	import { onDestroy, type Component } from 'svelte';
	import type { SmartlistSummary } from '$lib/rb/api-smartlists';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { refuseTrackDragOver, trackDrag } from '$lib/rb/track-drag.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import { requestTracksRefresh } from '$lib/rb/tracks-refresh-request';
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

	// LIBM-84 (issue #12307): a track drag over a smartlist row is refused
	// (no-drop cursor, no preventDefault, so no drop and no playlist request).
	let refusedId: string | null = $state(null);

	function refuseDragOver(event: DragEvent, sl: SmartlistSummary): void {
		if (refuseTrackDragOver(event)) refusedId = sl.id;
	}

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

	/** SMART (#10024): the smartlist whose rule editor modal is open, or null. */
	let ruleEditing = $state<{ id: string; name: string } | null>(null);

	// The rule editor exists only after Edit rule... or a double-click, so its module (the whole
	// editor, about 12 KB gzip) is fetched by that pick instead of riding in /performance. A failed
	// fetch stays failed in this document, so the only recovery is Reload (see TrackRowPopovers).
	type RuleModalProps = { smartlist: { id: string; name: string }; onclose: () => void };
	let RuleModal: Component<RuleModalProps> | null = $state(null);
	let ruleLoadError: string | null = $state(null);
	let ruleModalRequested = false;

	$effect(() => {
		if (ruleEditing === null || ruleModalRequested) return;
		ruleModalRequested = true;
		import('../SmartlistRuleModal.svelte')
			.then((m) => {
				RuleModal = m.default;
			})
			.catch((exc: unknown) => {
				console.error('[smartlists] rule editor failed to load', exc);
				ruleLoadError = exc instanceof Error ? exc.message : String(exc);
			});
	});

	export function editRule(sl: { id: string; name: string }): void {
		ruleEditing = { id: sl.id, name: sl.name };
	}

	/** Re-evaluate now: re-read the tree counts and refetch the open panes' rows. */
	export async function refreshNow(sl: { id: string; name: string }): Promise<void> {
		await smartlists.reload();
		if (smartlists.error !== null) {
			pushToast(`refresh failed: ${smartlists.error}`, 'error');
			return;
		}
		requestTracksRefresh([]);
		pushToast(`Refreshed smartlist "${sl.name}"`, 'info');
	}
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
	refusedId={trackDrag.active ? refusedId : null}
	ontoggle={() => smartlists.toggle()}
	onclickrow={(sl) => smartlists.click(sl)}
	ondblclickrow={editRule}
	onmenukey={(e, sl) => treeContextMenu?.openSmartlistFromKeyboard(e, sl)}
	oncontextmenurow={(e, sl) => treeContextMenu?.openSmartlist(e, sl)}
	oneditdraft={(value) => (rename.editDraft = value)}
	onrenamekeydown={(e) => rename.onKeydown(e)}
	onrenamecommit={() => void rename.commit()}
	ondragoverrow={refuseDragOver}
	ondragleaverow={(sl) => {
		if (refusedId === sl.id) refusedId = null;
	}}
	ondroprow={() => (refusedId = null)}
/>

{#if ruleEditing !== null && RuleModal !== null}
	<RuleModal smartlist={ruleEditing} onclose={() => (ruleEditing = null)} />
{:else if ruleEditing !== null && ruleLoadError !== null}
	<div class="rule-load-error" role="alert">
		<span>Rule editor failed to load: {ruleLoadError}</span>
		<button type="button" onclick={() => location.reload()}>Reload</button>
		<button type="button" onclick={() => (ruleEditing = null)}>Close</button>
	</div>
{/if}

<style>
	.rule-load-error {
		position: fixed;
		top: 50%;
		left: 50%;
		z-index: 1001;
		display: flex;
		gap: 8px;
		align-items: center;
		max-width: 360px;
		padding: 6px 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-red);
		border-radius: 4px;
		color: var(--rb-red);
		font-size: 11px;
		overflow-wrap: anywhere;
		transform: translate(-50%, -50%);
	}
</style>
