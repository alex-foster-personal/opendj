<script lang="ts">
	import type { SmartlistSummary } from '$lib/rb/api-smartlists';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { pushToast } from '$lib/stores.svelte';
	import type TreeContextMenu from './TreeContextMenu.svelte';
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

	$effect(() => {
		onDeleteReady?.(deleteSmartlistUi);
	});
</script>

<div
	class="row folder"
	role="button"
	tabindex="0"
	onclick={() => smartlists.toggle()}
	onkeydown={(e) => {
		if (e.key === 'Enter') smartlists.toggle();
	}}
>
	<span class="disclosure" class:open={smartlists.open}>&#9656;</span>
	<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
		<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
	</svg>
	<span class="name">Smartlists</span>
</div>
{#if smartlists.open}
	{#if smartlists.error !== null}
		<div class="row child rb-inert" title={smartlists.error}>
			<span class="name error">smartlists unavailable</span>
		</div>
	{:else if smartlists.rows === null}
		<div class="row child rb-inert">
			<span class="name dim">...</span>
		</div>
	{:else}
		{#each smartlists.rows as sl (sl.id)}
			<div
				class="row child"
				class:rb-inert={!onselectsmartlist}
				class:selected={selectedId === sl.id}
				role="button"
				tabindex="0"
				data-testid="smartlist-row"
				title={onselectsmartlist
					? sl.rule_summary
					: 'not implemented - see PARITY-TODO'}
				onclick={() => smartlists.click(sl)}
				onkeydown={(e) => {
					if (e.key === 'Enter') smartlists.click(sl);
					treeContextMenu?.openSmartlistFromKeyboard(e, sl);
				}}
				oncontextmenu={(e) => treeContextMenu?.openSmartlist(e, sl)}
			>
				<svg class="gear" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path
						d="M8 5.2A2.8 2.8 0 1 0 8 10.8 2.8 2.8 0 0 0 8 5.2zm0 4.3a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm6-.5.1-1-1.5-.6-.2-.6.8-1.4-.7-.7-1.4.8-.6-.3L9.9 3.7h-1L8.3 5.2l-.6.3-1.4-.8-.7.7.8 1.4-.3.6-1.5.5v1l1.5.6.3.6-.8 1.4.7.7 1.4-.8.6.3.6 1.5h1l.6-1.5.6-.3 1.4.8.7-.7-.8-1.4.3-.6z"
						fill="currentColor"
					/>
				</svg>
				<span class="name" title={sl.rule_summary}>{sl.name}</span>
			</div>
		{/each}
	{/if}
{/if}

<style>
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding: 0 6px;
		color: var(--rb-text);
		cursor: pointer;
		white-space: nowrap;
	}
	.row:hover {
		background: var(--rb-panel-raised);
	}
	.row.selected {
		background: var(--rb-select);
	}
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.row.child {
		padding-left: 22px;
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.disclosure {
		flex: none;
		display: inline-block;
		width: 8px;
		color: var(--rb-text-dim);
		transition: transform 0.1s;
	}
	.disclosure.open {
		transform: rotate(90deg);
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
	.name.dim,
	.name.error {
		color: var(--rb-text-dim);
	}
	.gear {
		flex: none;
	}
</style>
