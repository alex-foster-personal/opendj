<script lang="ts">
	import type { PlaylistNode } from '$lib/rb/library-types';
	import ContextMenu, { type ContextMenuItem } from '../ContextMenu.svelte';

	let {
		oncreate,
		oncreatesmartlist,
		onrename,
		deleteNode,
		onduplicate,
		onforbidduplicates,
		onrenamesmartlist,
		onduplicatesmartlist,
		onselect,
		ondeletesmartlist
	}: {
		oncreate?: (() => void) | undefined;
		oncreatesmartlist?: (() => void) | undefined;
		onrename?: ((node: PlaylistNode) => void) | undefined;
		deleteNode?: ((node: PlaylistNode) => void) | undefined;
		onduplicate?: ((node: PlaylistNode) => void) | undefined;
		onforbidduplicates?: ((node: PlaylistNode) => void) | undefined;
		onrenamesmartlist?: ((sl: { id: string; name: string }) => void) | undefined;
		onduplicatesmartlist?: ((sl: { id: string; name: string }) => void) | undefined;
		onselect: (node: PlaylistNode) => void;
		ondeletesmartlist?: ((sl: { id: string; name: string }) => void) | undefined;
	} = $props();
	let menu = $state<{
		x: number;
		y: number;
		kind: 'playlist' | 'folder' | 'smartlist';
		node?: PlaylistNode | undefined;
		smartlist?: { id: string; name: string } | undefined;
	} | null>(null);

	function items(
		kind: 'playlist' | 'folder' | 'smartlist',
		node?: PlaylistNode,
		smartlist?: { id: string; name: string }
	): ContextMenuItem[] {
		const isSmartlist = kind === 'smartlist';
		// Unbuilt rows (New folder, Export, Import from Spotify, Pin offline,
		// Sort by..., and the inert Forbid duplicates placeholder on
		// non-playlists) are HIDDEN for V1 rather than shown inert (JIK, Thu 1
		// Oct 2026): no folder-create, export, Spotify, offline-pin or tree-sort
		// API exists to wire them to.
		return [
			{ id: 'new-playlist', label: 'New playlist', run: oncreate },
			{ id: 'new-smartlist', label: 'New smartlist', run: oncreatesmartlist },
			{
				id: 'rename',
				label: 'Rename',
				run:
					isSmartlist && smartlist !== undefined
						? () => onrenamesmartlist?.(smartlist)
						: !isSmartlist && node !== undefined
							? () => onrename?.(node)
							: undefined
			},
			{
				id: 'delete',
				label: 'Delete',
				run:
					isSmartlist && smartlist !== undefined
						? () => ondeletesmartlist?.(smartlist)
						: node === undefined
							? undefined
							: () => deleteNode?.(node)
			},
			{
				id: 'duplicate',
				label: 'Duplicate',
				run:
					isSmartlist && smartlist !== undefined
						? () => onduplicatesmartlist?.(smartlist)
						: kind === 'playlist' && node !== undefined
							? () => onduplicate?.(node)
							: undefined
			},
			...(kind === 'playlist' && node !== undefined
				? [
						{
							id: 'forbid-duplicates',
							label: 'Forbid duplicates',
							checked: node.forbid_duplicates === true,
							run: () => onforbidduplicates?.(node)
						}
					]
				: []),
			{ id: 'reveal', label: 'Reveal in tree', run: kind === 'playlist' && node !== undefined ? () => onselect(node) : undefined }
		];
	}

	export function open(event: MouseEvent, kind: 'playlist' | 'folder', node?: PlaylistNode): void {
		event.preventDefault();
		event.stopPropagation();
		menu = { x: event.clientX, y: event.clientY, kind, node };
	}

	export function openSmartlist(
		event: MouseEvent,
		sl: { id: string; name: string }
	): void {
		event.preventDefault();
		event.stopPropagation();
		menu = { x: event.clientX, y: event.clientY, kind: 'smartlist', smartlist: sl };
	}

	export function openFromKeyboard(event: KeyboardEvent, kind: 'playlist' | 'folder', node?: PlaylistNode): void {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		event.preventDefault();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		menu = { x: rect.left + 8, y: rect.top + 8, kind, node };
	}

	export function openSmartlistFromKeyboard(
		event: KeyboardEvent,
		sl: { id: string; name: string }
	): void {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		event.preventDefault();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		menu = { x: rect.left + 8, y: rect.top + 8, kind: 'smartlist', smartlist: sl };
	}
</script>

{#if menu !== null}
	<ContextMenu
		items={items(menu.kind, menu.node, menu.smartlist)}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}
