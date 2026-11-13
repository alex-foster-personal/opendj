/** Track context-menu remove-from-library copy (LIBM-52 / LIBM-101b). */

export type TrackLibraryMenuItem = {
	id: 'remove-library';
	label: string;
	run?: () => void;
};

/** Scope of Remove from library: the database record goes, the audio file stays (LIBM-101b). */
export const REMOVE_FROM_LIBRARY_SCOPE =
	'Removes the database record only. The audio file is not deleted.';

/** Scope of Remove from playlist: the track leaves this playlist and stays in the library. */
export const REMOVE_FROM_PLAYLIST_SCOPE =
	'Removes the track from this playlist only. It stays in the library and the audio file is not touched.';

export function removeFromLibraryConfirmMessage(count: number): string {
	if (count === 1) {
		return 'Remove 1 track from the library? This removes the database record only. The audio file is not deleted and stays on disk.';
	}
	return `Remove ${count} tracks from the library? This removes the database records only. The audio files are not deleted and stay on disk.`;
}

export function removeFromLibraryToastMessage(count: number): string {
	if (count === 1) {
		return 'Removed 1 track from the library. The file stays on disk.';
	}
	return `Removed ${count} tracks from the library. Files stay on disk.`;
}

/** Confirm a remove-from-library batch; return the deduplicated ids to remove, or none when cancelled. */
export function confirmRemoveFromLibrary(
	stableIds: string[],
	confirm: (message: string) => boolean
): string[] {
	const ids = [...new Set(stableIds)];
	if (ids.length === 0) return [];
	return confirm(removeFromLibraryConfirmMessage(ids.length)) ? ids : [];
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

export type RestoreDeletedOutcome = {
	restored: string[];
	failed: { stableId: string; error: string }[];
	message: string;
	tone: 'info' | 'error';
};

/** Restore deleted tracks one by one; report which came back and why any failed (LIBM-54). */
export async function restoreDeletedTracks(
	stableIds: string[],
	undelete: (stableId: string) => Promise<unknown>
): Promise<RestoreDeletedOutcome> {
	const restored: string[] = [];
	const failed: { stableId: string; error: string }[] = [];
	for (const stableId of [...new Set(stableIds)]) {
		try {
			await undelete(stableId);
			restored.push(stableId);
		} catch (exc) {
			failed.push({ stableId, error: exc instanceof Error ? exc.message : String(exc) });
		}
	}
	if (failed.length > 0) {
		const first = failed[0];
		return {
			restored,
			failed,
			message: `Restore failed for ${failed.length} track${failed.length === 1 ? '' : 's'} (already in the library or missing): ${first.error}`,
			tone: 'error'
		};
	}
	return {
		restored,
		failed,
		message:
			restored.length === 1
				? 'Restored 1 track to the library.'
				: `Restored ${restored.length} tracks to the library.`,
		tone: 'info'
	};
}
