/** Track context-menu remove-from-library copy (LIBM-52 / LIBM-101b). */

export type TrackLibraryMenuItem = {
	id: 'remove-library';
	label: string;
	run?: () => void;
};

export function removeFromLibraryConfirmMessage(count: number): string {
	if (count === 1) {
		return 'Remove 1 track from the library? The file stays on disk.';
	}
	return `Remove ${count} tracks from the library? Files stay on disk.`;
}

export function removeFromLibraryToastMessage(count: number): string {
	if (count === 1) {
		return 'Removed 1 track from the library. The file stays on disk.';
	}
	return `Removed ${count} tracks from the library. Files stay on disk.`;
}

export function removeFromLibraryMenuItem(
	selectedIds: string[],
	run: ((ids: string[]) => void) | undefined
): TrackLibraryMenuItem {
	return run
		? {
				id: 'remove-library',
				label: 'Remove from library',
				run: () => run(selectedIds)
			}
		: {
				id: 'remove-library',
				label: 'Remove from library'
			};
}
