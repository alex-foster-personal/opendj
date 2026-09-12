/** Track context-menu items that open the toolbar track-edit modals (LIBM-62/66/68). */

export type TrackEditModalKind = 'bulk-edit' | 'find-replace' | 'mytag';

export const TRACK_EDIT_SELECTION_REQUIRED_TITLE = 'select at least one track first';

export type TrackEditMenuItem = {
	id: TrackEditModalKind;
	label: string;
	run?: () => void;
	title?: string;
};

export function trackEditMenuItems(
	selectedCount: number,
	openEditModal: ((kind: TrackEditModalKind) => void) | undefined
): TrackEditMenuItem[] {
	function item(id: TrackEditModalKind, label: string, needsSelection: boolean): TrackEditMenuItem {
		if (needsSelection && selectedCount === 0) {
			return { id, label, title: TRACK_EDIT_SELECTION_REQUIRED_TITLE };
		}
		if (openEditModal === undefined) return { id, label };
		return { id, label, run: () => openEditModal(id) };
	}
	return [
		item('bulk-edit', `Bulk edit (${selectedCount})`, true),
		item('find-replace', 'Find/replace', true),
		item('mytag', 'My Tag editor', false)
	];
}
