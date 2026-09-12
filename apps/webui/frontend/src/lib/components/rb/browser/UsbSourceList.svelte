<script lang="ts">
	import { openUsbPanel, presentNonForgotten } from '$lib/rb/usb-tracker.svelte';

	const volumes = $derived(presentNonForgotten());
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
				onclick={() => openUsbPanel()}
				onkeydown={(e) => {
					if (e.key === 'Enter') openUsbPanel();
				}}
			>
				<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path d="M4 2h8v12H4zM6 4h4v2H6z" fill="currentColor" />
				</svg>
				<span class="name" title={vol.name}>{vol.name}</span>
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
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
</style>
