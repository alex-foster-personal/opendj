/**
 * LIBWS-01: shared state between the Library workspace route (Crate Desk)
 * and the browser it mounts.
 *
 * The browser (BrowserPanel) is the same component Gig mode mounts under
 * its decks. In Library mode it publishes its active pane's selection here
 * so the inspector and preview bar can follow it, and it refuses deck loads:
 * Library mode advertises "no decks, no workers" (app-mode.ts), and a deck
 * load would build the audio engine the mode switch just tore down.
 */

import type { BrowserRow } from '$lib/components/rb/browser/pane-contract.svelte';

export interface LibraryWorkspaceState {
	/** True while the Library workspace route is mounted. */
	active: boolean;
	/** Selected stable_ids of the browser's active pane, in selection order. */
	selected_ids: string[];
	/** The hydrated rows for selected_ids, one per id (first membership wins). */
	selected_rows: BrowserRow[];
	/** Active pane title, for the inspector header. */
	pane_title: string;
}

export const libraryWorkspace: LibraryWorkspaceState = $state({
	active: false,
	selected_ids: [],
	selected_rows: [],
	pane_title: ''
});

export const LIBRARY_DECK_LOAD_REFUSAL =
	'Library mode has no decks. Switch to Gig to load a track.';

export function enterLibraryWorkspace(): void {
	libraryWorkspace.active = true;
}

export function leaveLibraryWorkspace(): void {
	libraryWorkspace.active = false;
	libraryWorkspace.selected_ids = [];
	libraryWorkspace.selected_rows = [];
	libraryWorkspace.pane_title = '';
}

/** Rows for `ids` in id order, from the pane's loaded rows. */
export function rowsForSelection(
	ids: readonly string[],
	rows: readonly BrowserRow[]
): BrowserRow[] {
	const byId = new Map<string, BrowserRow>();
	for (const row of rows) {
		if (!byId.has(row.stable_id)) byId.set(row.stable_id, row);
	}
	const picked: BrowserRow[] = [];
	for (const id of ids) {
		const row = byId.get(id);
		if (row !== undefined) picked.push(row);
	}
	return picked;
}

export function publishLibrarySelection(
	ids: readonly string[],
	rows: readonly BrowserRow[],
	paneTitle: string
): void {
	if (!libraryWorkspace.active) return;
	libraryWorkspace.selected_ids = [...ids];
	libraryWorkspace.selected_rows = rowsForSelection(ids, rows);
	libraryWorkspace.pane_title = paneTitle;
}
