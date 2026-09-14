/** Track context-menu "Relocate" copy (FLOW-07).
 *
 * The `relocate` item shipped with no `run` at all - it rendered as an
 * inert row forever, while the only working relocate flow lived on the
 * disconnected `/reconcile` page. This gives the menu item the same
 * open-a-popover pattern `show-in-playlists` (LIBM-29) already uses, so the
 * dead-end is fixed at the exact place a user would try it: mid-browse,
 * right-clicking the broken row. */

export type TrackRelocateMenuItem = {
	id: 'relocate';
	label: string;
	run?: () => void;
};

export function relocateMenuItem(open: () => void): TrackRelocateMenuItem {
	return {
		id: 'relocate',
		label: 'Relocate',
		run: open
	};
}
