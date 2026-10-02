<script lang="ts">
	import ContextMenu, { type ContextMenuItem } from '../ContextMenu.svelte';
	import {
		ADD_TO_PLAYLIST_EMPTY_TITLE,
		ADD_TO_PLAYLIST_TITLE,
		addToPlaylistMenuItem
	} from './add-to-playlist-menu';
	import type { BrowserRow } from './pane-contract.svelte';
	import {
		COPY_PATH_TITLE,
		loadDeckTitle,
		menuTargetIds,
		REANALYZE_TITLE,
		runCopyPaths,
		runReanalyze,
		runRevealTracks,
		REVEAL_TRACK_TITLE
	} from './track-context-menu';
	import {
		trackEditMenuItems,
		type TrackEditModalKind
	} from './track-edit-menu';
	import { removeFromLibraryMenuItem } from './track-library-menu';
	import { showInPlaylistsMenuItem } from './track-playlists-menu';
	import { relocateMenuItem } from './track-relocate-menu';

	type DeckId = 1 | 2 | 3 | 4;
	const DECKS: DeckId[] = [1, 2, 3, 4];

	let {
		selectedIds,
		selectedOrders,
		onselectrow,
		onloadrow,
		onaddtoplaylist,
		onstemsdonext,
		onlyricsdonext,
		onopeneditmodal,
		onremovefromlibrary,
		onremoverow,
		removable = false,
		onshowinplaylists,
		onrelocate
	}: {
		selectedIds: string[];
		selectedOrders: number[];
		onselectrow: (row: BrowserRow) => void;
		onloadrow: (row: BrowserRow, deck: DeckId) => void;
		onaddtoplaylist?: ((stableIds: string[]) => void) | undefined;
		onstemsdonext?: ((stableIds: string[]) => void) | undefined;
		onlyricsdonext?: ((stableIds: string[]) => void) | undefined;
		onopeneditmodal?: ((kind: TrackEditModalKind) => void) | undefined;
		onremovefromlibrary?: ((stableIds: string[]) => void) | undefined;
		onremoverow?: ((row: BrowserRow) => void) | undefined;
		removable?: boolean;
		onshowinplaylists: (row: BrowserRow, x: number, y: number) => void;
		onrelocate: (row: BrowserRow, x: number, y: number) => void;
	} = $props();

	let menu = $state<{
		x: number;
		y: number;
		row: BrowserRow;
		targetIds: string[];
	} | null>(null);

	function openMenu(
		x: number,
		y: number,
		row: BrowserRow,
		selectedOrderSet: Set<number>
	): void {
		if (!selectedOrderSet.has(row.order)) onselectrow(row);
		menu = {
			x,
			y,
			row,
			targetIds: menuTargetIds(row, selectedOrders, selectedIds)
		};
	}

	export function open(event: MouseEvent, row: BrowserRow): void {
		event.preventDefault();
		event.stopPropagation();
		openMenu(event.clientX, event.clientY, row, new Set(selectedOrders));
	}

	export function openFromKeyboard(event: KeyboardEvent, row: BrowserRow): void {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		event.preventDefault();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		openMenu(rect.left + 8, rect.top + 8, row, new Set(selectedOrders));
	}

	function trackMenuItems(
		row: BrowserRow,
		targetIds: string[],
		menuX: number,
		menuY: number
	): ContextMenuItem[] {
		const addItem = addToPlaylistMenuItem(targetIds, onaddtoplaylist);
		// Unbuilt rows with no handler and no feature behind them (a bare "Edit"
		// label, Stems - open, Mark offline, Cloud-only) are HIDDEN for V1 rather
		// than shown inert (JIK, Thu 1 Oct 2026). Rows whose `run` is undefined
		// only in context (Remove from playlist outside a playlist, Stems/Lyrics
		// when the host passes no handler) stay, since the feature is real.
		return [
			...DECKS.map((deck) => ({
				id: `load-${deck}`,
				label: `Load to deck ${deck}`,
				testId: `context-menu-load-deck-${deck}`,
				title: loadDeckTitle(deck, row.stable_id),
				run: () => onloadrow(row, deck)
			})),
			{
				...addItem,
				title: addItem.run ? ADD_TO_PLAYLIST_TITLE : ADD_TO_PLAYLIST_EMPTY_TITLE
			},
			...trackEditMenuItems(targetIds.length, onopeneditmodal),
			relocateMenuItem(() => onrelocate(row, menuX, menuY)),
			{
				id: 'finder',
				label: 'Show in Finder',
				title: REVEAL_TRACK_TITLE,
				run: () => runRevealTracks(targetIds)
			},
			showInPlaylistsMenuItem(() => onshowinplaylists(row, menuX, menuY)),
			{
				id: 'copy-path',
				label: 'Copy path',
				title: COPY_PATH_TITLE,
				run: () => runCopyPaths(targetIds)
			},
			{
				id: 'analyze',
				label: 'Re-analyze',
				title: REANALYZE_TITLE,
				run: () => runReanalyze(targetIds)
			},
			{
				id: 'stems-generate',
				label: 'Stems: do next',
				run: onstemsdonext ? () => onstemsdonext(targetIds) : undefined
			},
			{
				id: 'lyrics',
				label: 'Lyrics: do next',
				run: onlyricsdonext ? () => onlyricsdonext(targetIds) : undefined
			},
			{
				id: 'remove-playlist',
				label: 'Remove from playlist',
				run: removable ? () => onremoverow?.(row) : undefined
			},
			removeFromLibraryMenuItem(targetIds, onremovefromlibrary)
		];
	}
</script>

{#if menu !== null}
	<ContextMenu
		items={trackMenuItems(menu.row, menu.targetIds, menu.x, menu.y)}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}
