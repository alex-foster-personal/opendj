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
		usbTracker,
		type UsbVolumeKnown
	} from '$lib/rb/usb-tracker.svelte';
	import UsbStickTree from './UsbStickTree.svelte';
	import ContextMenu from '../ContextMenu.svelte';
	import { loadMenuRegistry, menuRegistry } from '$lib/context-menu/lazy.svelte';
	import { usbStickMenuItems } from '$lib/rb/usb-stick-menu';
	import { reportMenuLoadFailure } from './track-context-menu';
	import { canImportUsbVolume, importUsbVolume } from '$lib/rb/usb-import';
	import { openUsbExport } from '$lib/rb/usb-export.svelte';

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

	/**
	 * USBEXP-04: the stick row carries the same right-click menu (USB status,
	 * Sync, Eject) as its tree, so a stick that is collapsed, or that macOS has
	 * not let Open DJ read and so never expands, still offers it (Codex P2
	 * 4302000930). stopPropagation keeps Quick Draw's window listener on
	 * /performance from replacing it.
	 */
	let menu = $state<{ vol: UsbVolumeKnown; x: number; y: number } | null>(null);

	function _openRowMenu(event: MouseEvent, vol: UsbVolumeKnown): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		menu = { vol, x: event.clientX, y: event.clientY };
	}

	function _rowKeydown(event: KeyboardEvent, vol: UsbVolumeKnown): void {
		if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
			loadMenuRegistry(reportMenuLoadFailure);
			event.preventDefault();
			event.stopPropagation();
			const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
			menu = { vol, x: rect.left + 8, y: rect.top + 8 };
		} else if (event.key === 'Enter') {
			_toggleStick(vol);
		}
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
				oncontextmenu={(e) => _openRowMenu(e, vol)}
				onkeydown={(e) => _rowKeydown(e, vol)}
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
	<!-- USBEXP-01: the USB export screen's home in the USB source view. It
	     lists blank sticks too, which this list folds away as not music. -->
	<button
		type="button"
		class="row export-row"
		data-testid="usb-source-export"
		title="USB export - pick a stick, write an export to it, or eject it"
		onclick={openUsbExport}
	>
		<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
			<path d="M8 3l5 6H3zM3 11h10v2H3z" fill="currentColor" />
		</svg>
		<span class="name">Export / eject...</span>
	</button>
	{#if menu !== null && menuRegistry() !== null}
		<ContextMenu
			items={usbStickMenuItems(menuRegistry()!.menuFor, menu.vol, usbTracker)}
			x={menu.x}
			y={menu.y}
			onclose={() => (menu = null)}
		/>
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
	.export-row {
		width: 100%;
		margin-top: 4px;
		background: transparent;
		border: none;
		border-top: 1px solid var(--rb-border);
		font: inherit;
		text-align: left;
		color: var(--rb-text-dim);
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
