/**
 * Sections of the lists on screen, and the writes that change them. The server
 * is the only source of truth: every write re-reads, so a drop can never leave
 * the table showing an order the server refused.
 *
 * A set is a `kind = 'set'` playlist whose sections are owned as
 * `('set', playlist_id)`. The server says which, so this never guesses.
 *
 * CLI: `opendj api GET /api/v1/playlists/{id}/sections`,
 * `opendj api POST|PATCH|DELETE /api/v1/sections...` and
 * `opendj api POST /api/v1/playlists/{id}/entries:move`.
 */
import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
import {
	deleteSection,
	getListSections,
	libraryWriteMessage,
	patchSectionTitle,
	postEntriesMove,
	postSection
} from '$lib/rb/playlist-write';
import type { DropTarget, PlaylistSection } from './playlist-sections';
import { undoSessionId } from './undo-session';

export interface EntriesMove extends DropTarget {
	item_ids: string[];
	section_ids: string[];
}

type OwnerKind = 'playlist' | 'set';

const NONE: readonly PlaylistSection[] = [];
const byList = $state<Record<string, { ownerKind: OwnerKind; rows: readonly PlaylistSection[] }>>({});

export const sectionWriteMessage = libraryWriteMessage;

export function sectionsOf(listId: string | null): readonly PlaylistSection[] {
	return listId === null ? NONE : (byList[listId]?.rows ?? NONE);
}

/** Keep every list on screen current when an agent or another window edits its
 * dividers: those writes change no membership, so no ETag turns over. */
export function watchSections(): () => void {
	const reloadAll = (): void => {
		for (const listId of Object.keys(byList)) {
			void loadSections(listId);
		}
	};
	const stopKind = subscribeKind('sections', reloadAll);
	const stopResync = subscribeResync(reloadAll);
	return () => {
		stopKind();
		stopResync();
	};
}

/** REORDER-ROW-02: show dividers where an optimistic move put them, until the
 * next loadSections reads the server's answer. A list never loaded has none. */
export function showSectionsLocally(listId: string, rows: readonly PlaylistSection[]): void {
	const entry = byList[listId];
	if (entry !== undefined) byList[listId] = { ownerKind: entry.ownerKind, rows };
}

export async function loadSections(listId: string): Promise<void> {
	const { owner_kind, rows } = await getListSections(listId);
	byList[listId] = { ownerKind: owner_kind, rows };
}

export async function addSection(listId: string, beforePosition: number): Promise<string> {
	if (byList[listId] === undefined) await loadSections(listId);
	const { row } = await postSection(undoSessionId(), {
		owner_kind: byList[listId].ownerKind,
		owner_id: listId,
		before_position: beforePosition
	});
	await loadSections(listId);
	return String(row.id);
}

export async function renameSection(listId: string, sectionId: string, title: string): Promise<void> {
	await patchSectionTitle(undoSessionId(), sectionId, title);
	await loadSections(listId);
}

export async function removeSection(listId: string, sectionId: string): Promise<void> {
	await deleteSection(undoSessionId(), sectionId);
	await loadSections(listId);
}

export async function moveEntries(listId: string, move: EntriesMove): Promise<void> {
	await postEntriesMove(undoSessionId(), listId, move);
}
