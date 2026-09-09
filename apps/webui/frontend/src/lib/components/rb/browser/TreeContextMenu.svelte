<script lang="ts">
	import type { PlaylistNode } from '$lib/rb/library-types';
	import ContextMenu, { type ContextMenuItem } from '../ContextMenu.svelte';

	let {
		oncreate,
		onrename,
		deleteNode,
		onselect
	}: {
		oncreate?: (() => void) | undefined;
		onrename?: ((node: PlaylistNode) => void) | undefined;
		deleteNode?: ((node: PlaylistNode) => void) | undefined;
		onselect: (node: PlaylistNode) => void;
	} = $props();
	let menu = $state<{ x: number; y: number; kind: 'playlist' | 'folder'; node?: PlaylistNode | undefined } | null>(null);

	function items(kind: 'playlist' | 'folder', node?: PlaylistNode): ContextMenuItem[] {
		return [
			{ id: 'new-playlist', label: 'New playlist', run: oncreate }, { id: 'new-folder', label: 'New folder' },
			{ id: 'rename', label: 'Rename', run: node === undefined ? undefined : () => onrename?.(node) },
			{ id: 'delete', label: 'Delete', run: node === undefined ? undefined : () => deleteNode?.(node) },
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

	export function openFromKeyboard(event: KeyboardEvent, kind: 'playlist' | 'folder', node?: PlaylistNode): void {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		event.preventDefault();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		menu = { x: rect.left + 8, y: rect.top + 8, kind, node };
	}
</script>

{#if menu !== null}
	<ContextMenu items={items(menu.kind, menu.node)} x={menu.x} y={menu.y} onclose={() => (menu = null)} />
{/if}
