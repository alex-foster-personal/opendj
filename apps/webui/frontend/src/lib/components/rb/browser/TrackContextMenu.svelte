<script module lang="ts">
	// TrackTable reaches the tag surface here: it sits at the fan-out ceiling, and
	// this menu already hosts the tag editor and its confirm.
	import {
		tagTypeChooserEntries,
		tagTypeColumns,
		toggleTagTypeColumn,
		toggleTagTypeInTags
	} from '$lib/rb/library-tags.svelte';
	import TagCell from './TagCell.svelte';

	export {
		TagCell,
		tagTypeChooserEntries,
		tagTypeColumns,
		toggleTagTypeColumn,
		toggleTagTypeInTags
	};
</script>

<script lang="ts">
	import { onMount } from 'svelte';
	import { setTargetState } from '$lib/rb/set-target-state.svelte';
	import ContextMenu from '../ContextMenu.svelte';
	import ColumnChooserMenu, { type ColumnChooserPrefs } from './ColumnChooserMenu.svelte';
	import { bindMenuItems, type BoundMenuItem, type MenuRunners } from '$lib/context-menu/bind';
	import {
		loadMenuRegistry,
		menuRegistry,
		preloadMenuRegistry
	} from '$lib/context-menu/lazy.svelte';
	import { ensureTagsLoaded, openTagEditor, tagState } from '$lib/rb/library-tags.svelte';
	import { isUsbTrackId } from '$lib/rb/track-source';
	import TagEditorPopover from './TagEditorPopover.svelte';
	import TagRemoveConfirm from './TagRemoveConfirm.svelte';
	import type { BrowserRow } from './pane-contract.svelte';
	import {
		menuItemsForTrack,
		menuTargetIds,
		menuTargetRowCount,
		reportMenuLoadFailure,
		runCopyPaths,
		runPairTracks,
		runReanalyze,
		runRevealTracks,
		runPlaylistShuffle,
		runSetColour,
		runSetRating,
		runSetTracklistHidden,
		type PlaylistShuffleDeps
	} from './track-context-menu';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import type { TrackEditModalKind } from './track-edit-menu';
	import { libraryWorkspace } from '$lib/library/library-workspace.svelte';
	import { linkVersionsFromMenu, openWorkEditor } from '$lib/rb/works-menu';
	import { offloadOps } from '$lib/offload/offload-ops.svelte';
	import { startTitleFixScanForIds } from '$lib/rb/title-fix-scan';
	import { restoreDeletedTracks } from './track-library-menu';
	import { undeleteFromLibrary } from '$lib/rb/track-library';
	import { pushToast } from '$lib/stores.svelte';

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
		oninsertsection,
		removable = false,
		readOnly = false,
		deleted = false,
		onshowinplaylists,
		onrelocate,
		oneditcomment = undefined,
		onplaylistshuffle = undefined,
		shuffleDeps = null
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
		oninsertsection?: ((row: BrowserRow) => void) | undefined;
		removable?: boolean;
		/** Read-only pane: its rows are not writable, so pairing is hidden (PAIR-10). */
		readOnly?: boolean;
		/** LIBM-54: rows of the deleted-tracks view; the menu offers Restore to library. */
		deleted?: boolean;
		onshowinplaylists: (row: BrowserRow, x: number, y: number) => void;
		onrelocate: (row: BrowserRow, x: number, y: number) => void;
		/** LIBUX-56: open the row's Comment cell editor. */
		oneditcomment?: ((row: BrowserRow) => void) | undefined;
		/** Issue #11325: Shuffle playlist from the # column header menu. */
		onplaylistshuffle?: (() => void) | undefined;
		shuffleDeps?: PlaylistShuffleDeps | null;
	} = $props();

	onMount(ensureTagsLoaded);

	/** LIBM-54: restore deleted rows; the server's library.changed event refreshes the view. */
	async function restoreFromLibraryUi(stableIds: string[]): Promise<void> {
		const outcome = await restoreDeletedTracks(stableIds, undeleteFromLibrary);
		pushToast(outcome.message, outcome.tone);
	}

	let menu = $state<{
		x: number;
		y: number;
		row: BrowserRow;
		targetIds: string[];
		rowCount: number;
		/** Items shown above the row's own menu (a tempo or key cell puts its Edit here). */
		lead: BoundMenuItem[];
	} | null>(null);

	function openMenu(
		x: number,
		y: number,
		row: BrowserRow,
		selectedOrderSet: Set<number>,
		lead: BoundMenuItem[] = []
	): void {
		if (!selectedOrderSet.has(row.order)) onselectrow(row);
		// One right-click menu at a time: a track menu closes the header's chooser.
		chooserAt = null;
		menu = {
			x,
			y,
			row,
			targetIds: menuTargetIds(row, selectedOrders, selectedIds),
			rowCount: menuTargetRowCount(row, selectedOrders),
			lead
		};
	}

	$effect(preloadMenuRegistry);

	/** LIBUX-54: the header's column chooser, one of the table's right-click menus. */
	let chooserAt = $state<{ x: number; y: number; prefs: ColumnChooserPrefs } | null>(null);
	let orderHeaderAt = $state<{ x: number; y: number } | null>(null);

	export function openColumnChooser(event: MouseEvent, prefs: ColumnChooserPrefs): void {
		event.preventDefault();
		menu = null;
		orderHeaderAt = null;
		chooserAt = { x: event.clientX, y: event.clientY, prefs };
	}

	/** Right-click the # column header (pin 3b217ec7698c, issue #11325). */
	export function openOrderHeaderMenu(event: MouseEvent): void {
		event.preventDefault();
		event.stopPropagation();
		menu = null;
		chooserAt = null;
		orderHeaderAt = { x: event.clientX, y: event.clientY };
	}

	/** `lead` items sit above the row's menu: a tempo or key cell adds its Edit there, so a
	 * right-click on that cell still offers every row action (add to playlist, remove...). */
	export function open(event: MouseEvent, row: BrowserRow, lead: BoundMenuItem[] = []): void {
		loadMenuRegistry(reportMenuLoadFailure);
		event.preventDefault();
		event.stopPropagation();
		openMenu(event.clientX, event.clientY, row, new Set(selectedOrders), lead);
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
					: deleted
						? 'deleted'
						: readOnly
						? 'readonly-import'
						: removable
							? 'playlist'
							: 'collection',
				audio: trackAudioOf(row),
				stems: row.stems?.status === 'ready' ? 'ready' : 'none',
				tracklistHidden: row.tracklist_hidden ? 'hidden' : 'shown',
				// LIBWS-03/06: Library mode hides worker starters; deck loads hand the track to Gig.
				workspace: libraryWorkspace.active ? 'library' : 'performance'
			}
		);
		const editModal = (kind: TrackEditModalKind): (() => void) | undefined =>
			onopeneditmodal ? () => onopeneditmodal(kind) : undefined;
		const runners: MenuRunners = {
			...Object.fromEntries(DECKS.map((deck) => [`load-${deck}`, () => onloadrow(row, deck)])),
			'track-info': () => {
				if (libraryWorkspace.active) {
					document
						.querySelector('[data-testid="track-inspector"]')
						?.scrollIntoView({ block: 'nearest' });
					return;
				}
				// PRRT_kwDOSEvNd860wEnM: same typed dispatcher as an agent's track_info_open.
				void runPerformanceCommandFromUi({
					type: 'track_info_open',
					stable_id: targetIds.length === 1 ? targetIds[0]! : row.stable_id
				});
			},
			...Object.fromEntries(
				[0, 1, 2, 3, 4, 5].map((n) => [`rate-${n}`, () => runSetRating(targetIds, n)])
			),
			...Object.fromEntries(
				[0, 1, 2, 3, 4, 5, 6, 7, 8].map((n) => [
					`colour-${n}`,
					() => runSetColour(targetIds, n === 0 ? null : n)
				])
			),
			'add-playlist': onaddtoplaylist ? () => onaddtoplaylist(targetIds) : undefined,
			// Issue #10028: same typed dispatcher as an agent's tag_list_add.
			'add-tag-list': () =>
				void runPerformanceCommandFromUi({ type: 'tag_list_add', stable_ids: targetIds }),
			'bulk-edit': editModal('bulk-edit'),
			'find-replace': editModal('find-replace'),
			'add-tag': () => openTagEditor({ stableIds: targetIds, typeId: null, x: menuX, y: menuY }),
			mytag: editModal('mytag'),
			'edit-comment': oneditcomment ? () => oneditcomment(row) : undefined,
			'tracklist-hidden': () => runSetTracklistHidden(targetIds, !row.tracklist_hidden),
			relocate: () => onrelocate(row, menuX, menuY),
			finder: () => runRevealTracks(targetIds),
			'show-in-playlists': () => onshowinplaylists(row, menuX, menuY),
			'copy-path': () => runCopyPaths(targetIds),
			'work-edit': () => openWorkEditor(targetIds, row.title ?? ''),
			'link-version': () => linkVersionsFromMenu(targetIds, row.title ?? ''),
			analyze: () => runReanalyze(targetIds),
			pair: () => runPairTracks(targetIds),
			'stems-generate': onstemsdonext ? () => onstemsdonext(targetIds) : undefined,
			lyrics: onlyricsdonext ? () => onlyricsdonext(targetIds) : undefined,
			'title-fix-scan': () => void startTitleFixScanForIds(targetIds),
			'set-skip': () => void setTargetState.toggleSkip(targetIds),
			'remove-playlist': onremoverow ? () => onremoverow(row) : undefined,
			'insert-section': oninsertsection ? () => oninsertsection(row) : undefined,
			'remove-library': onremovefromlibrary ? () => onremovefromlibrary(targetIds) : undefined,
			'restore-library': () => void restoreFromLibraryUi(targetIds),
			offload: () => void offloadOps.offloadTracks(targetIds),
			'offload-restore-24h': () => void offloadOps.restoreTracks(targetIds, 'temporary'),
			'offload-restore-permanent': () => void offloadOps.restoreTracks(targetIds, 'permanent')
		};
		// The stick allowlist stays as a second, independent guard.
		return menuItemsForTrack(row.stable_id, bindMenuItems(resolved, runners));
	}
</script>

{#if tagState.editor !== null}
	{#key tagState.editor}
		<TagEditorPopover {...tagState.editor} />
	{/key}
{/if}
{#if tagState.removal !== null}
	<TagRemoveConfirm request={tagState.removal} />
{/if}
{#if chooserAt !== null}
	<ColumnChooserMenu x={chooserAt.x} y={chooserAt.y} prefs={chooserAt.prefs} onclose={() => (chooserAt = null)} />
{/if}
{#if orderHeaderAt !== null}
	<ContextMenu
		items={[
			{
				id: 'playlist-shuffle',
				label: 'Shuffle',
				title:
					shuffleDeps === null || shuffleDeps.allowed
						? 'Reshuffle playlist membership; recently played tracks in Open DJ are weighted lower'
						: (shuffleDeps?.blockedReason ?? 'Shuffle is unavailable'),
				run:
					shuffleDeps !== null && shuffleDeps.allowed
						? () => {
								if (onplaylistshuffle) onplaylistshuffle();
								else void runPlaylistShuffle(shuffleDeps);
							}
						: undefined
			}
		]}
		x={orderHeaderAt.x}
		y={orderHeaderAt.y}
		onclose={() => (orderHeaderAt = null)}
	/>
{/if}
{#if menu !== null && menuRegistry() !== null}
	<ContextMenu
		items={[
			...menu.lead,
			...trackMenuItems(menuRegistry()!, menu.row, menu.targetIds, menu.rowCount, menu.x, menu.y)
		]}
		x={menu.x}
		y={menu.y}
		onclose={() => (menu = null)}
	/>
{/if}
