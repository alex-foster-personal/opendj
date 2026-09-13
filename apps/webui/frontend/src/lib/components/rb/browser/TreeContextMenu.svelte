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
		return [
			{ id: 'new-playlist', label: 'New playlist', run: oncreate },
			{ id: 'new-smartlist', label: 'New smartlist', run: oncreatesmartlist },
			{ id: 'new-folder', label: 'New folder' },
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
			{
				id: 'forbid-duplicates',
				label: 'Forbid duplicates',
				checked: kind === 'playlist' && node !== undefined
					? node.forbid_duplicates === true
					: undefined,
				run:
					kind === 'playlist' && node !== undefined
						? () => onforbidduplicates?.(node)
						: undefined
			},
			{ id: 'export', label: 'Export' }, { id: 'spotify', label: 'Import from Spotify' },
			{ id: 'offline', label: 'Pin offline' }, { id: 'sort', label: 'Sort by...' },
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
