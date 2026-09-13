<script lang="ts">
	import { openUsbPanel, presentNonForgotten } from '$lib/rb/usb-tracker.svelte';
	import { canImportUsbVolume, importUsbVolume } from '$lib/rb/usb-import';

	const volumes = $derived(presentNonForgotten());

	const IMPORT_TITLE =
		'Import tracks from this stick. Files stay on the stick. Tags only - no BPM, key, or beatgrid.';
</script>

<div class="usb-source-root">
	{#if volumes.length === 0}
		<div class="row rb-inert">
			<span class="name dim">No USB sticks</span>
		</div>
	{:else}
		{#each volumes as vol (vol.id)}
			<div
				class="row"
				role="button"
				tabindex="0"
				data-testid="usb-source-row"
				onclick={() => openUsbPanel(vol.id)}
				onkeydown={(e) => {
					if (e.key === 'Enter') openUsbPanel(vol.id);
				}}
			>
				<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path d="M4 2h8v12H4zM6 4h4v2H6z" fill="currentColor" />
				</svg>
				<span class="name" title={vol.name}>{vol.name}</span>
				<button
					type="button"
					class="import-btn"
					data-testid="usb-source-import"
					title={IMPORT_TITLE}
					disabled={!canImportUsbVolume(vol)}
					onclick={(e) => {
						e.stopPropagation();
						void importUsbVolume(vol);
					}}
				>
					Import
				</button>
			</div>
		{/each}
	{/if}
</div>

<style>
	.usb-source-root {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 2px 0;
	}
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
	.row svg {
		flex: none;
		color: var(--rb-text-dim);
	}
	.name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.name.dim {
		color: var(--rb-text-dim);
	}
	.import-btn {
		flex: none;
		padding: 0 4px;
		font-size: 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		cursor: pointer;
	}
	.import-btn:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
</style>
