<script lang="ts">
	/**
	 * Play from USB (USBPLAY-05): one plugged-in stick's tree, drawn under its
	 * row in the USBs tab. All tracks, then the stick's playlists and folders
	 * in rekordbox order, then History. Selecting a list opens it in the
	 * active browser pane through the same `onselect` the library trees use
	 * (browser_select_playlist, so agents drive it too).
	 *
	 * Lazy-loaded by UsbSourceList on the first stick click, and it reads the
	 * stick only then: mounting this component IS the user's click.
	 */
	import { onMount } from 'svelte';
	import type { PlaylistNode } from '$lib/rb/library-types';
	import {
		openUsbStick,
		toggleUsbFolder,
		usbErrorHeadline,
		usbLibrary
	} from '$lib/rb/usb-library.svelte';
	import { usbTracker, type UsbVolumeKnown } from '$lib/rb/usb-tracker.svelte';
	import UsbStickTreeView from './UsbStickTreeView.svelte';
	import ContextMenu from '../ContextMenu.svelte';
	import { loadMenuRegistry, menuRegistry } from '$lib/context-menu/lazy.svelte';
	import { usbStickMenuItems } from '$lib/rb/usb-stick-menu';
	import { reportMenuLoadFailure } from './track-context-menu';

	let {
		volume,
		selectedId,
		onselect
	}: {
		volume: UsbVolumeKnown;
		selectedId: string | null;
		onselect: (node: PlaylistNode) => void;
	} = $props();

	const view = $derived(usbLibrary.sticks[volume.id]);

	onMount(() => {
		// Mounting is the click. A cached stick opens instantly; an unplugged
		// one (cache dropped) or a failed read is read again, so collapsing
		// and re-opening a stick row is also its retry.
		if (view?.status !== 'ready' && view?.status !== 'loading') openUsbStick(volume);
	});

	function _activate(node: PlaylistNode): void {
		if (node.kind === 'folder') toggleUsbFolder(node.playlist_id);
		else onselect(node);
	}

	/**
	 * USBEXP-04: the stick's own right-click menu (USB status, Sync, Eject),
	 * opened here rather than by Quick Draw so Library mode, which has no Quick
	 * Draw, shows the same rows. stopPropagation keeps Quick Draw's window
	 * listener on /performance from replacing it. No preload of its own: the
	 * playlist tree's TreeContextMenu, mounted in both workspaces, already
	 * warms the registry, and a cold right-click loads it on open.
	 */
	let menu = $state<{ x: number; y: number } | null>(null);

	function _openMenu(event: MouseEvent): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		menu = { x: event.clientX, y: event.clientY };
	}

	function _openMenuFromKeyboard(event: KeyboardEvent): boolean {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return false;
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		menu = { x: rect.left + 8, y: rect.top + 8 };
		return true;
	}

	function _keydown(event: KeyboardEvent, node: PlaylistNode): void {
		if (_openMenuFromKeyboard(event)) return;
		if (event.key !== 'Enter' && event.key !== ' ') return;
		event.preventDefault();
		_activate(node);
	}
</script>

<div
	class="usb-stick-tree"
	role="tree"
	tabindex="-1"
	aria-label="{volume.name} playlists"
	data-testid="usb-stick-tree"
	data-menu-target="tree-readonly"
	oncontextmenu={_openMenu}
>
	<UsbStickTreeView
		volumeName={volume.name}
		{view}
		openFolders={usbLibrary.openFolders}
		errorHeadline={view?.status === 'error' ? usbErrorHeadline(view.code) : ''}
		{selectedId}
		onactivate={_activate}
		onrowkeydown={_keydown}
	/>
</div>

{#if menu !== null && menuRegistry() !== null}
	<ContextMenu
		items={usbStickMenuItems(menuRegistry()!.menuFor, volume, usbTracker)}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}
