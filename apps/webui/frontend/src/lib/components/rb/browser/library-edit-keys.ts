/**
 * Cmd/Ctrl+A, C, X, V on the track list (pins ce142ae7e22f, 0b1e12cc01d0).
 *
 * Pure rules live in ./track-clipboard; this owns the key and the one write.
 * Paste goes through the atomic transfer endpoint, which is a set-union on
 * the destination, so a repeated Cmd+V never stacks duplicates and a cut
 * paste removes the tracks from their source in the same transaction.
 * BrowserPanel supplies the pane, row and reload accessors it owns. The
 * clipboard rules load on the first shortcut press (main's #4904 payback on
 * the /performance bundle), so only the key-to-action rule loads eagerly.
 */

import type { PlaylistNode } from '$lib/rb/library-types';
import {
	getPlaylistTracksEtag,
	PlaylistConflictError,
	transferPlaylistTracks
} from '$lib/rb/playlist-write';
import type { PaneStore } from './pane-contract.svelte';
import type { RowRef } from './pane-row-selection';
import { libraryEditShortcut } from './library-edit-shortcut';
import { scrollTopForRowIndex, TRACK_TABLE_THEAD_PX } from './virtual-window';

export interface LibraryEditKeyDeps {
	/** The active pane, and every pane (for reloads after a paste). */
	pane(): PaneStore;
	panes(): readonly PaneStore[];
	/** The rows the user sees in the active pane (post filter and sort). */
	renderedRows(): readonly RowRef[];
	source(): 'collection' | 'spotify';
	/** TrackTable's row height for the current density. */
	rowHeight(): number;
	currentNode(p: PaneStore): PlaylistNode | null;
	loadPane(p: PaneStore, node: PlaylistNode): Promise<void>;
	refreshPlaylists(): Promise<void>;
	/** Bump the navigation epoch so the remembered scroll is applied. */
	bumpNavEpoch(): void;
	/** The app toast (passed in so this module adds no stores importer). */
	pushToast(message: string, kind: 'info' | 'error'): void;
}

export function createLibraryEditKeys(deps: LibraryEditKeyDeps): (e: KeyboardEvent) => void {
	const pushToast = deps.pushToast;
	function onLibraryEditKey(e: KeyboardEvent): void {
		if (e.repeat) return;
		const action = libraryEditShortcut(e);
		if (action === null) return;
		// A modal dialog over the library owns the keyboard.
		if (document.querySelector('dialog[open], [aria-modal="true"]') !== null) return;
		if (action === 'copy' || action === 'cut') {
			// Selected page text (a lyric line, a toast) keeps the native copy.
			const sel = window.getSelection();
			if (sel !== null && !sel.isCollapsed && sel.toString().trim() !== '') return;
		}
		e.preventDefault();
		void _runLibraryEdit(action, deps.pane());
	}

	async function _runLibraryEdit(action: 'select_all' | 'copy' | 'cut' | 'paste', p: PaneStore): Promise<void> {
		const { clipboardToastMessage, selectAllRows, selectedIdsInViewOrder, setTrackClipboard } =
			await import('./track-clipboard');
		if (action === 'select_all') {
			if (selectAllRows(p, deps.renderedRows()) === 0) pushToast('no tracks to select', 'info');
			return;
		}
		if (action === 'copy' || action === 'cut') {
			const ids = selectedIdsInViewOrder(deps.renderedRows(), p.selected_orders, p.selected_ids);
			if (ids.length === 0) {
				pushToast(`select tracks to ${action} first`, 'info');
				return;
			}
			const fromPlaylist =
				deps.source() === 'collection' && p.kind === 'playlist' && p.playlist_id !== null && !p.whole_collection
					? p.playlist_id
					: null;
			// Cutting from something that is not a playlist (All Tracks,
			// search results) has nothing to remove the tracks from: copy.
			const mode = action === 'cut' && fromPlaylist !== null ? 'cut' : 'copy';
			setTrackClipboard({
				stable_ids: ids,
				mode,
				source_playlist_id: fromPlaylist,
				source_title: p.title
			});
			pushToast(clipboardToastMessage(ids.length, mode), 'info');
			return;
		}
		await _pasteTracks(p);
	}

	async function _pasteTracks(p: PaneStore): Promise<void> {
		const { getTrackClipboard, partitionPaste, pasteBlockReason, pasteToastMessage, setTrackClipboard } =
			await import('./track-clipboard');
		const clip = getTrackClipboard();
		const blocked = pasteBlockReason(p, deps.source(), clip);
		if (blocked !== null || clip === null || p.playlist_id === null) {
			pushToast(blocked ?? 'nothing to paste', 'info');
			return;
		}
		const destId = p.playlist_id;
		const destTitle = p.title;
		const move = clip.mode === 'cut' && clip.source_playlist_id !== null && clip.source_playlist_id !== destId;
		try {
			const dest = await getPlaylistTracksEtag(destId);
			const plan = partitionPaste(
				clip.stable_ids,
				dest.detail.tracks.map((t) => t.stable_id)
			);
			if (plan.add.length > 0 || move) {
				const src = move && clip.source_playlist_id !== null
					? await getPlaylistTracksEtag(clip.source_playlist_id)
					: null;
				await transferPlaylistTracks(destId, dest.etag, {
					stable_ids: clip.stable_ids,
					mode: move ? 'move' : 'add',
					...(src !== null && clip.source_playlist_id !== null
						? { source_playlist_id: clip.source_playlist_id, source_etag: src.etag }
						: {})
				});
			}
			// A cut pastes once, like a Finder move; copy can paste again.
			if (move) setTrackClipboard({ ...clip, mode: 'copy', source_playlist_id: null });
			pushToast(pasteToastMessage(plan.add.length, plan.already, destTitle, move), 'info');
			// Reload every pane showing either playlist BEFORE the tree refresh,
			// so the pasted rows are on screen as soon as the write lands.
			const sourceId = move ? clip.source_playlist_id : null;
			await Promise.all(
				deps.panes()
					.filter((q) => q.playlist_id === destId || (sourceId !== null && q.playlist_id === sourceId))
					.map(async (q) => {
						const node = deps.currentNode(q);
						if (node !== null) await deps.loadPane(q, node);
					})
			);
			if (p.playlist_id === destId && plan.add.length > 0) await _revealPasted(p, plan.add);
		} catch (exc) {
			if (exc instanceof PlaylistConflictError) {
				pushToast('playlist changed elsewhere - press Cmd+V again to paste into the latest version', 'error');
			} else {
				pushToast(`paste failed: ${String(exc)}`, 'error');
			}
			return;
		}
		await deps.refreshPlaylists();
	}

	/** Pasted rows land at the END of the playlist, below the fold in a long
	 * one: select them and scroll the first into view so the paste is seen. */
	async function _revealPasted(p: PaneStore, pastedIds: string[]): Promise<void> {
		const { pastedRowOrders, selectRowOrders } = await import('./track-clipboard');
		const orders = pastedRowOrders(p.rows, pastedIds);
		if (orders.length === 0) return;
		selectRowOrders(p, orders);
		if (p !== deps.pane()) return;
		const index = deps.renderedRows().findIndex((r) => r.order === orders[0]);
		if (index < 0) return;
		p.rememberScroll(
			scrollTopForRowIndex({
				rowIndex: Math.max(0, index - 2),
				rowHeight: deps.rowHeight(),
				headerOffsetPx: TRACK_TABLE_THEAD_PX
			})
		);
		deps.bumpNavEpoch();
	}

	return onLibraryEditKey;
}
