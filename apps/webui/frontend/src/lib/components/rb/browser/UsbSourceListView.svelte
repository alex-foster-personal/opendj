<script module lang="ts">
	/** System Settings > Privacy & Security > Files and Folders, where macOS
	 * keeps the Removable Volumes switch (USBPLAY-02). Same deep-link scheme
	 * as the preflight remediation links (PreflightCheckRow.svelte). */
	const FILES_AND_FOLDERS_URL =
		'x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders';
</script>

<script lang="ts">
	// UsbSourceList's rows as props only: the container reads the USB tracker
	// and owns which sticks are open; an open stick's tree comes in as the
	// stickTree snippet, since that tree reads the stick itself.
	import type { Snippet } from 'svelte';
	import type { UsbVolumeKnown } from '$lib/rb/usb-tracker.svelte';

	let {
		volumes,
		openSticks,
		canImport,
		importTitle,
		importButtonLabel = 'Import',
		ontoggle,
		ondetails,
		onimport,
		onrowcontextmenu,
		onrowkeydown,
		stickTree
	}: {
		volumes: UsbVolumeKnown[];
		/** Sticks the user opened, by volume id. */
		openSticks: Record<string, boolean>;
		canImport: (vol: UsbVolumeKnown) => boolean;
		importTitle: string;
		importButtonLabel?: string;
		ontoggle: (vol: UsbVolumeKnown) => void;
		ondetails: (volumeId: string) => void;
		onimport: (vol: UsbVolumeKnown) => void;
		onrowcontextmenu: (event: MouseEvent, vol: UsbVolumeKnown) => void;
		onrowkeydown: (event: KeyboardEvent, vol: UsbVolumeKnown) => void;
		stickTree: Snippet<[UsbVolumeKnown]>;
	} = $props();
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
				class:selected={openSticks[vol.id]}
				role="button"
				tabindex="0"
				aria-expanded={openSticks[vol.id] === true}
				data-testid="usb-source-row"
				data-access={vol.access ?? 'unknown'}
				onclick={() => ontoggle(vol)}
				oncontextmenu={(e) => onrowcontextmenu(e, vol)}
				onkeydown={(e) => onrowkeydown(e, vol)}
			>
				<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path d="M4 2h8v12H4zM6 4h4v2H6z" fill="currentColor" />
				</svg>
				<span class="name" title={vol.name}>{vol.name}</span>
				<button
					type="button"
					class="row-btn"
					data-testid="usb-source-details"
					title="Stick details: name, yours, forget"
					onclick={(e) => {
						e.stopPropagation();
						ondetails(vol.id);
					}}
				>
					...
				</button>
				<button
					type="button"
					class="row-btn"
					data-testid="usb-source-import"
					title={importTitle}
					disabled={!canImport(vol)}
					onclick={(e) => {
						e.stopPropagation();
						onimport(vol);
					}}
				>
					{importButtonLabel}
				</button>
			</div>
			{#if vol.access === 'pending'}
				<div
					class="access"
					data-testid="usb-access-pending"
					title="macOS is asking whether Open DJ may read removable volumes. Answer that prompt to browse this stick."
				>
					waiting for permission
				</div>
			{:else if vol.access === 'denied'}
				<div
					class="access denied"
					data-testid="usb-access-denied"
					title="macOS refused Open DJ access to removable volumes. Turn on Removable Volumes for Open DJ in System Settings > Privacy & Security > Files and Folders."
				>
					<span>Open DJ cannot read this drive</span>
					<a href={FILES_AND_FOLDERS_URL} data-testid="usb-access-settings">Open System Settings</a>
				</div>
			{:else if openSticks[vol.id]}
				{@render stickTree(vol)}
			{/if}
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
	.row.selected {
		color: var(--rb-accent);
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
	.row-btn {
		flex: none;
		padding: 0 4px;
		font-size: 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		cursor: pointer;
	}
	.row-btn:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.access {
		display: flex;
		flex-wrap: wrap;
		gap: 2px 6px;
		padding: 1px 6px 3px 23px;
		font-size: 10px;
		color: var(--rb-text-dim);
	}
	.access.denied {
		color: var(--rb-red);
	}
	.access a {
		color: var(--rb-accent);
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
</style>
