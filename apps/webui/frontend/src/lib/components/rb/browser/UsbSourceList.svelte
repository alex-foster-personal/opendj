<script module lang="ts">
	/**
	 * The USBs tab body (lazy-loaded by LibraryNav on the tab's first visit,
	 * so this list, the stick tree and the Play from USB store all stay out
	 * of the /performance first-paint bundle).
	 */

	/** Sticks the user opened, by volume id. Module scope so a tab switch or an
	 * unplug-replug keeps a stick open (USBPLAY-09: its tree comes back). */
	const openSticks = $state<Record<string, boolean>>({});

	/** System Settings > Privacy & Security > Files and Folders, where macOS
	 * keeps the Removable Volumes switch (USBPLAY-02). Same deep-link scheme
	 * as the preflight remediation links (PreflightCheckRow.svelte). */
	const FILES_AND_FOLDERS_URL =
		'x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders';
</script>

<script lang="ts">
	import type { PlaylistNode } from '$lib/rb/library-types';
	import {
		openUsbPanel,
		presentNonForgotten,
		usbAccessBlocked,
		type UsbVolumeKnown
	} from '$lib/rb/usb-tracker.svelte';
	import UsbStickTree from './UsbStickTree.svelte';
	import { canImportUsbVolume, importUsbVolume } from '$lib/rb/usb-import';

	let {
		selectedId,
		onselect
	}: {
		selectedId: string | null;
		onselect: (node: PlaylistNode) => void;
	} = $props();

	const volumes = $derived(presentNonForgotten());

	const IMPORT_TITLE =
		'Import tracks from this stick. Files stay on the stick. Tags only - no BPM, key, or beatgrid.';

	function _toggleStick(vol: UsbVolumeKnown): void {
		if (usbAccessBlocked(vol)) return;
		openSticks[vol.id] = !openSticks[vol.id];
	}
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
				onclick={() => _toggleStick(vol)}
				onkeydown={(e) => {
					if (e.key === 'Enter') _toggleStick(vol);
				}}
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
						openUsbPanel(vol.id);
					}}
				>
					...
				</button>
				<button
					type="button"
					class="row-btn"
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
				<UsbStickTree volume={vol} {selectedId} {onselect} />
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
