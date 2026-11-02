<script lang="ts">
	import ContextMenu from '../ContextMenu.svelte';
	import ColumnChooserMenu, { type ColumnChooserPrefs } from './ColumnChooserMenu.svelte';
	import { bindMenuItems, type BoundMenuItem, type MenuRunners } from '$lib/context-menu/bind';
	import { loadMenuRegistry, menuRegistry, preloadMenuRegistry } from '$lib/context-menu/lazy.svelte';
	import { isUsbTrackId } from '$lib/rb/track-source';
	import type { BrowserRow } from './pane-contract.svelte';
	import {
		menuItemsForTrack,
		menuTargetIds,
		menuTargetRowCount,
		reportMenuLoadFailure,
		runCopyPaths,
		runPairTracks,
		runReanalyze,
		runRevealTracks
	} from './track-context-menu';
	import type { TrackEditModalKind } from './track-edit-menu';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';

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
		readOnly = false,
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
		/** Read-only pane: its rows are not writable, so pairing is hidden (PAIR-10). */
		readOnly?: boolean;
		onshowinplaylists: (row: BrowserRow, x: number, y: number) => void;
		onrelocate: (row: BrowserRow, x: number, y: number) => void;
	} = $props();

	let menu = $state<{
		x: number;
		y: number;
		row: BrowserRow;
		targetIds: string[];
		rowCount: number;
	} | null>(null);

	function openMenu(
		x: number,
		y: number,
		row: BrowserRow,
		selectedOrderSet: Set<number>
	): void {
		if (!selectedOrderSet.has(row.order)) onselectrow(row);
		// One right-click menu at a time: a track menu closes the header's chooser.
		chooserAt = null;
		menu = {
			x,
			y,
			row,
			targetIds: menuTargetIds(row, selectedOrders, selectedIds),
			rowCount: menuTargetRowCount(row, selectedOrders)
		};
	}

	$effect(preloadMenuRegistry);

	/** LIBUX-54: the header's column chooser, one of the table's right-click menus. */
	let chooserAt = $state<{ x: number; y: number; prefs: ColumnChooserPrefs } | null>(null);

	export function openColumnChooser(event: MouseEvent, prefs: ColumnChooserPrefs): void {
		event.preventDefault();
		menu = null;
		chooserAt = { x: event.clientX, y: event.clientY, prefs };
	}

	export function open(event: MouseEvent, row: BrowserRow): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		openMenu(event.clientX, event.clientY, row, new Set(selectedOrders));
	}

	export function openFromKeyboard(event: KeyboardEvent, row: BrowserRow): void {
		if (event.key !== 'ContextMenu' && !(event.shiftKey && event.key === 'F10')) return;
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		openMenu(rect.left + 8, rect.top + 8, row, new Set(selectedOrders));
	}

	/**
	 * What shows, and whether it is enabled, comes from the W28 registry
	 * (`$lib/context-menu/registry.ts`, golden matrix in
	 * (private path)). This component only gathers the row's
	 * context and binds each item id to its runner. An item whose runner the
	 * host did not pass renders disabled with a reason rather than vanishing.
	 */
	function trackMenuItems(
		{ menuFor, trackAudioOf }: NonNullable<ReturnType<typeof menuRegistry>>,
		row: BrowserRow,
		targetIds: string[],
		rowCount: number,
		menuX: number,
		menuY: number
	): BoundMenuItem[] {
		const resolved = menuFor(
			{ kind: 'track', count: targetIds.length, rows: rowCount },
			{
				// Rows, not distinct ids: two rows of one track are still a multi-row selection.
				selection: rowCount > 1 ? 'many' : 'one',
				source: isUsbTrackId(row.stable_id)
					? 'usb'
					: readOnly
						? 'readonly-import'
						: removable
							? 'playlist'
							: 'collection',
				audio: trackAudioOf(row),
				stems: row.stems?.status === 'ready' ? 'ready' : 'none',
				// LIBWS-03: the registry drops deck loads and worker starters here.
				workspace: libraryWorkspace.active ? 'library' : 'performance'
			}
		);
		const editModal = (kind: TrackEditModalKind): (() => void) | undefined =>
			onopeneditmodal ? () => onopeneditmodal(kind) : undefined;
		const runners: MenuRunners = {
			...Object.fromEntries(DECKS.map((deck) => [`load-${deck}`, () => onloadrow(row, deck)])),
			'add-playlist': onaddtoplaylist ? () => onaddtoplaylist(targetIds) : undefined,
			'bulk-edit': editModal('bulk-edit'),
			'find-replace': editModal('find-replace'),
			mytag: editModal('mytag'),
			relocate: () => onrelocate(row, menuX, menuY),
			finder: () => runRevealTracks(targetIds),
			'show-in-playlists': () => onshowinplaylists(row, menuX, menuY),
			'copy-path': () => runCopyPaths(targetIds),
			analyze: () => runReanalyze(targetIds),
			pair: () => runPairTracks(targetIds),
			'stems-generate': onstemsdonext ? () => onstemsdonext(targetIds) : undefined,
			lyrics: onlyricsdonext ? () => onlyricsdonext(targetIds) : undefined,
			'remove-playlist': onremoverow ? () => onremoverow(row) : undefined,
			'remove-library': onremovefromlibrary ? () => onremovefromlibrary(targetIds) : undefined
		};
		// The stick allowlist stays as a second, independent guard.
		return menuItemsForTrack(row.stable_id, bindMenuItems(resolved, runners));
	}
</script>

{#if chooserAt !== null}
	<ColumnChooserMenu x={chooserAt.x} y={chooserAt.y} prefs={chooserAt.prefs} onclose={() => (chooserAt = null)} />
{/if}
{#if menu !== null && menuRegistry() !== null}
	<ContextMenu
		items={trackMenuItems(menuRegistry()!, menu.row, menu.targetIds, menu.rowCount, menu.x, menu.y)}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}
