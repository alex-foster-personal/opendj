<script module lang="ts">
	/**
	 * The USBs tab body (lazy-loaded by LibraryNav on the tab's first visit,
	 * so this list, the stick tree and the Play from USB store all stay out
	 * of the /performance first-paint bundle).
	 */

	/** Sticks the user opened, by volume id. Module scope so a tab switch or an
	 * unplug-replug keeps a stick open (USBPLAY-09: its tree comes back). */
	const openSticks = $state<Record<string, boolean>>({});
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
	import UsbSourceListView from './UsbSourceListView.svelte';
	import UsbStickTree from './UsbStickTree.svelte';
	import ContextMenu from '../ContextMenu.svelte';
	import { loadMenuRegistry, menuRegistry } from '$lib/context-menu/lazy.svelte';
	import { usbStickMenuItems } from '$lib/rb/usb-stick-menu';
	import { reportMenuLoadFailure } from './track-context-menu';
	import { canImportUsbVolume, importUsbVolume, USB_IMPORT_TITLE } from '$lib/rb/usb-import';
	import { openUsbExport } from '$lib/rb/usb-export.svelte';

	let {
		selectedId,
		onselect
	}: {
		selectedId: string | null;
		onselect: (node: PlaylistNode) => void;
	} = $props();

	const volumes = $derived(presentNonForgotten());

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

{#snippet stickTree(vol: UsbVolumeKnown)}
	<UsbStickTree volume={vol} {selectedId} {onselect} />
{/snippet}

<div class="usb-source-shell">
	<UsbSourceListView
		{volumes}
		{openSticks}
		canImport={canImportUsbVolume}
		importTitle={USB_IMPORT_TITLE}
		importButtonLabel="Open"
		ontoggle={_toggleStick}
		ondetails={openUsbPanel}
		onimport={(vol) => void importUsbVolume(vol)}
		onrowcontextmenu={(e, vol) => _openRowMenu(e, vol)}
		onrowkeydown={(e, vol) => _rowKeydown(e, vol)}
		{stickTree}
	/>
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
	.usb-source-shell {
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
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
</style>
