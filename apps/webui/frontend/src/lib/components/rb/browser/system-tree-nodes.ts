/**
 * LIBM-104 (issue #12326): the tree's system nodes, Recently deleted and
 * Missing Tracks. Open DJ builds them, so they sit in their own section with
 * their own icons, and Rename and Delete stay visible on them but refuse,
 * with the reason reachable by focus and by a screen reader.
 */
export type SystemNodeAction = 'rename' | 'delete';

export const SYSTEM_SECTION_LABEL = 'System';

/** Missing Tracks: a page with an exclamation mark cut out (even-odd fill). */
export const MISSING_TRACKS_ICON_PATH =
	'M3 1h7l3 3v11H3zM7.25 4.5h1.5v5h-1.5zM7.25 10.75h1.5v1.5h-1.5z';

/** Recently deleted: a bin. */
export const RECENTLY_DELETED_ICON_PATH = 'M5.5 1h5v1.5H14V4H2V2.5h3.5zM3 5h10l-1 10H4z';

/** Why a system node refuses Rename and Delete, for its title and accessible description. */
export function systemNodeLockReason(name: string): string {
	return `${name} is a system list Open DJ keeps up to date, so it cannot be renamed or deleted`;
}

/** Invoke Rename or Delete on a system node: always refused, nothing changes, the reason says why. */
export function invokeSystemNodeAction(
	action: SystemNodeAction,
	name: string
): { performed: false; action: SystemNodeAction; reason: string } {
	return { performed: false, action, reason: systemNodeLockReason(name) };
}
