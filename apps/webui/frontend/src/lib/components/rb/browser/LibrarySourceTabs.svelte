<script lang="ts">
	export type LibrarySourceTab = 'playlists' | 'taglists' | 'autolists' | 'usbs';

	let {
		active,
		onchange
	}: {
		active: LibrarySourceTab;
		onchange: (tab: LibrarySourceTab) => void;
	} = $props();

	const tabs: { id: LibrarySourceTab; label: string; testid: string }[] = [
		{ id: 'playlists', label: 'Playlists', testid: 'library-source-playlists' },
		{ id: 'taglists', label: 'Taglists', testid: 'library-source-taglists' },
		{ id: 'autolists', label: 'Autolists', testid: 'library-source-autolists' },
		{ id: 'usbs', label: 'USBs', testid: 'library-source-usbs' }
	];
</script>

<div class="view-tabs" role="tablist" aria-label="library sources" data-testid="library-source-tabs">
	{#each tabs as tab (tab.id)}
		<button
			type="button"
			class="vt"
			class:active={active === tab.id}
			role="tab"
			aria-selected={active === tab.id}
			data-testid={tab.testid}
			onclick={() => onchange(tab.id)}
		>
			{tab.label}
		</button>
	{/each}
</div>

<style>
	.view-tabs {
		display: flex;
		flex: none;
		border-bottom: 1px solid var(--rb-border);
	}
	.vt {
		flex: 1;
		padding: 3px 0;
		background: var(--rb-panel);
		border: none;
		border-right: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		cursor: pointer;
	}
	.vt:last-child {
		border-right: none;
	}
	.vt.active {
		color: var(--rb-text);
		background: var(--rb-panel-raised);
		border-bottom: 1px solid var(--rb-accent);
	}
</style>
