<script lang="ts">
	import type { PlaylistNode } from '$lib/rb/library-types';
	import ContextMenu from '../ContextMenu.svelte';
	import { bindMenuItems, type BoundMenuItem } from '$lib/context-menu/bind';
	import { loadMenuRegistry, menuRegistry, preloadMenuRegistry } from '$lib/context-menu/lazy.svelte';
	import { reportMenuLoadFailure } from './track-context-menu';
	import { openUsbExportForPlaylist } from '$lib/rb/usb-export.svelte';
	import { downloadPlaylistText } from '$lib/rb/playlist-text-export';
	import { offloadOps } from '$lib/offload/offload-ops.svelte';
	import { startTitleFixScanForPlaylist } from '$lib/rb/title-fix-scan';
	import { isInboxPlaylistId } from './inbox-id';

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
		ondeletesmartlist,
		oneditsmartlistrule,
		onrefreshsmartlist,
		onsortplaylists
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
		/** SMART (#10024): open the rule editor modal for this smartlist. */
		oneditsmartlistrule?: ((sl: { id: string; name: string }) => void) | undefined;
		/** SMART (#10024): re-evaluate now; the item is disabled until this settles. */
		onrefreshsmartlist?: ((sl: { id: string; name: string }) => Promise<void>) | undefined;
		onsortplaylists?: (() => void) | undefined;
	} = $props();
	let menu = $state<{
		x: number;
		y: number;
		kind: 'playlist' | 'folder' | 'smartlist';
		node?: PlaylistNode | undefined;
		smartlist?: { id: string; name: string } | undefined;
	} | null>(null);
	let refreshingSmartlistId = $state<string | null>(null);

	async function refreshSmartlist(sl: { id: string; name: string }): Promise<void> {
		if (onrefreshsmartlist === undefined) return;
		refreshingSmartlistId = sl.id;
		try {
			await onrefreshsmartlist(sl);
		} finally {
			if (refreshingSmartlistId === sl.id) refreshingSmartlistId = null;
		}
	}

	/**
	 * Items come from the W28 registry ((private path)*.md,
	 * smartlist.md); this component binds ids to the tree's handlers. The
	 * "Playlists" header is its own target, so it no longer shows dead
	 * Rename/Delete/Duplicate rows, and a folder node never offers the
	 * playlist-only Duplicate and Forbid duplicates.
	 */
	$effect(preloadMenuRegistry);

	function items(
		{ menuFor }: NonNullable<ReturnType<typeof menuRegistry>>,
		kind: 'playlist' | 'folder' | 'smartlist',
		node?: PlaylistNode,
		smartlist?: { id: string; name: string }
	): BoundMenuItem[] {
		const news = { 'new-playlist': oncreate, 'new-smartlist': oncreatesmartlist };
		if (kind === 'smartlist' && smartlist !== undefined) {
			return bindMenuItems(menuFor({ kind: 'smartlist' }, { refreshing: refreshingSmartlistId === smartlist.id }), {
				...news,
				'edit-rule': oneditsmartlistrule === undefined ? undefined : () => oneditsmartlistrule(smartlist),
				refresh: onrefreshsmartlist === undefined ? undefined : () => void refreshSmartlist(smartlist),
				rename: () => onrenamesmartlist?.(smartlist),
				delete: () => ondeletesmartlist?.(smartlist),
				duplicate: () => onduplicatesmartlist?.(smartlist)
			});
		}
		if (node === undefined) {
			return bindMenuItems(menuFor({ kind: 'playlists-root' }, {}), { ...news, 'sort-name': onsortplaylists });
		}
		const nodeRunners = {
			...news,
			rename: () => onrename?.(node),
			delete: () => deleteNode?.(node)
		};
		if (node.kind === 'folder') {
			return bindMenuItems(menuFor({ kind: 'playlist-folder' }, {}), nodeRunners);
		}
		if (isInboxPlaylistId(node.playlist_id)) {
			return bindMenuItems(menuFor({ kind: 'playlist' }, { forbidDuplicates: false }), {
				reveal: () => onselect(node)
			});
		}
		return bindMenuItems(menuFor({ kind: 'playlist' }, { forbidDuplicates: node.forbid_duplicates === true }), {
			...nodeRunners,
			duplicate: () => onduplicate?.(node),
			'forbid-duplicates': () => onforbidduplicates?.(node),
			reveal: () => onselect(node),
			'export-usb': () => void openUsbExportForPlaylist(node.name),
			'export-text': () => downloadPlaylistText(node.playlist_id),
			'title-fix-scan': () => void startTitleFixScanForPlaylist(node.playlist_id),
			'offload-playlist': () => void offloadOps.offloadPlaylist(node.playlist_id),
			'offload-playlist-restore-24h': () => void offloadOps.restorePlaylist(node.playlist_id, 'temporary'),
			'offload-playlist-restore-permanent': () => void offloadOps.restorePlaylist(node.playlist_id, 'permanent')
		});
	}

	export function open(event: MouseEvent, kind: 'playlist' | 'folder', node?: PlaylistNode): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		menu = { x: event.clientX, y: event.clientY, kind, node };
	}

	export function openSmartlist(
		event: MouseEvent,
		sl: { id: string; name: string }
	): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		menu = { x: event.clientX, y: event.clientY, kind: 'smartlist', smartlist: sl };
	}

	export function openFromKeyboard(event: KeyboardEvent, kind: 'playlist' | 'folder', node?: PlaylistNode): void {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		menu = { x: rect.left + 8, y: rect.top + 8, kind, node };
	}

	export function openSmartlistFromKeyboard(
		event: KeyboardEvent,
		sl: { id: string; name: string }
	): void {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		menu = { x: rect.left + 8, y: rect.top + 8, kind: 'smartlist', smartlist: sl };
	}
</script>

{#if menu !== null && menuRegistry() !== null}
	<ContextMenu
		items={items(menuRegistry()!, menu.kind, menu.node, menu.smartlist)}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}
