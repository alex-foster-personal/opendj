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

{#snippet branch(nodes: PlaylistNode[], depth: number)}
	{#each nodes as node (node.playlist_id)}
		{@const isFolder = node.kind === 'folder'}
		{@const open = usbLibrary.openFolders[node.playlist_id] === true}
		<div
			class="row"
			class:selected={!isFolder && selectedId === node.playlist_id}
			style="padding-left: {18 + depth * 12}px"
			role="treeitem"
			tabindex="0"
			aria-selected={!isFolder && selectedId === node.playlist_id}
			aria-expanded={isFolder ? open : undefined}
			data-testid={isFolder ? 'usb-stick-folder' : 'usb-stick-node'}
			data-pane-id={node.playlist_id}
			onclick={() => _activate(node)}
			onkeydown={(e) => _keydown(e, node)}
		>
			{#if isFolder}
				<svg class="chev" class:open viewBox="0 0 8 8" width="8" height="8" aria-hidden="true">
					<path d="M2 1l4 3-4 3z" fill="currentColor" />
				</svg>
			{:else}
				<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path d="M2 4h12v2H2zM2 8h12v2H2zM2 12h8v2H2z" fill="currentColor" />
				</svg>
			{/if}
			<span class="name" title={node.name}>{node.name}</span>
			<span
				class="count"
				title={isFolder
					? `${node.track_count} lists in this folder on the stick`
					: `${node.track_count} tracks in this list on the stick`}>{node.track_count}</span
			>
		</div>
		{#if isFolder && open}
			{@render branch(node.children, depth + 1)}
		{/if}
	{/each}
{/snippet}

<div class="usb-stick-tree" role="tree" tabindex="-1" aria-label="{volume.name} playlists" data-testid="usb-stick-tree" data-menu-target="tree-readonly" oncontextmenu={_openMenu}>
	{#if view === undefined || view.status === 'loading'}
		<div class="row rb-inert" style="padding-left: 18px" data-testid="usb-stick-loading">
			<span class="name dim">reading stick...</span>
		</div>
	{:else if view.status === 'error'}
		<div
			class="row rb-inert"
			style="padding-left: 18px"
			data-testid="usb-stick-error"
			data-code={view.code}
			title={view.message}
		>
			<span class="name dim">{usbErrorHeadline(view.code)}</span>
		</div>
	{:else}
		{@render branch(view.tree, 0)}
	{/if}
</div>

{#if menu !== null && menuRegistry() !== null}
	<ContextMenu
		items={usbStickMenuItems(menuRegistry()!.menuFor, volume, usbTracker)}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}

<style>
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding-right: 6px;
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
	.chev {
		transition: transform 80ms linear;
	}
	.chev.open {
		transform: rotate(90deg);
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
	.count {
		flex: none;
		min-width: 4ch;
		text-align: right;
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.row.rb-inert {
		opacity: 0.6;
		cursor: default;
	}
	.row.rb-inert:hover {
		background: transparent;
	}
</style>
