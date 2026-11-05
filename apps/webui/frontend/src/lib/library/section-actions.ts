/**
 * What the library panel does with sections: every UI gesture on a divider
 * lands here, and each one is a single call an agent can make over HTTP.
 */
import {
	addSection,
	loadSections,
	moveEntries,
	removeSection,
	renameSection,
	sectionWriteMessage,
	type EntriesMove
} from './playlist-sections-store.svelte';
import { sectionItemIds, type PlaylistSection, type SectionHeader } from './playlist-sections';

type Toast = (message: string, level: 'info' | 'error') => void;

export interface SectionActionDeps {
	/** The list writes go to, or null after saying why it cannot be edited. */
	editableListId: () => string | null;
	rows: () => readonly { order: number; item_id: string | null }[];
	sections: () => readonly PlaylistSection[];
	selectedOrders: () => readonly number[];
	setRenaming: (sectionId: string | null) => void;
	/** REORDER-ROW-02: show `move` now (rows, dividers, selection); the revert
	 * undoes exactly that. Null when it cannot be mirrored locally. */
	applyMoveLocally: (move: EntriesMove) => { revert: () => void } | null;
	/** Read the list's rows back without the blank loading state of a reload. */
	refreshListQuietly: () => Promise<void>;
	toast: Toast;
}

export function sectionActions(deps: SectionActionDeps) {
	async function write(run: (listId: string) => Promise<void>): Promise<void> {
		const listId = deps.editableListId();
		if (listId === null) return;
		try {
			await run(listId);
		} catch (exc) {
			deps.toast(`section update failed: ${sectionWriteMessage(exc)}`, 'error');
		}
	}

	/** Insert a divider and go straight to naming it (D46). */
	function insert(beforePosition: number, aboveSectionId?: string): void {
		void write(async (listId) => {
			const sectionId = await addSection(listId, beforePosition);
			if (aboveSectionId !== undefined) {
				await moveEntries(listId, {
					item_ids: [],
					section_ids: [sectionId],
					before_section_id: aboveSectionId
				});
				await loadSections(listId);
			}
			deps.setRenaming(sectionId);
		});
	}

	return {
		insert,
		insertAbove(header: SectionHeader): void {
			insert(header.section.before_position, header.section.id);
		},
		/** `+ Section`: above the one selected track, otherwise at the end. */
		addFromHeader(): void {
			const orders = deps.selectedOrders();
			insert(orders.length === 1 ? orders[0] - 1 : deps.rows().length);
		},
		rename(sectionId: string, title: string): void {
			deps.setRenaming(null);
			void write((listId) => renameSection(listId, sectionId, title));
		},
		remove(sectionId: string): void {
			void write((listId) => removeSection(listId, sectionId));
		},
		/** A section's tracks, hidden ones included, or null if one cannot move. */
		dragItems(sectionId: string): string[] | null {
			const ids = sectionItemIds(deps.rows(), deps.sections(), sectionId);
			if (ids.some((id) => id === null)) {
				deps.toast('playlist row has no membership id - reload and try again', 'error');
				return null;
			}
			return ids.filter((id) => id !== null);
		},
		move(move: EntriesMove): void {
			void write(async (listId) => {
				const optimistic = deps.applyMoveLocally(move);
				try {
					await moveEntries(listId, move);
				} catch (exc) {
					optimistic?.revert();
					throw exc;
				} finally {
					await Promise.all([deps.refreshListQuietly(), loadSections(listId)]);
				}
				// FLOW-06: a drag must look different from a dropped gesture.
				const count = move.item_ids.length;
				if (move.section_ids.length > 0) deps.toast('Moved section', 'info');
				else deps.toast(count === 1 ? 'Moved track' : `Moved ${count} tracks`, 'info');
			});
		}
	};
}
