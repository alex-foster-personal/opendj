/**
 * Disk-pref writes that must not report success they did not get (PR #4014,
 * Sol P1s on compatible-filter ranges, watcher folders and confirm resets).
 *
 * Each write runs as one step on the shared disk write chain with the verified
 * (throwing) PUT, and builds its value from the COMMITTED pref when that step
 * runs, not when it was queued. The live pref and localStorage change only
 * after the PUT lands. A failed step rejects to its own caller and commits
 * nothing, so a later queued step never carries the rejected change along
 * (Sol P2 r4167041284).
 */
import type { CompatibleFilterPrefs } from './compatible-filter-prefs';
import type { DiskPrefsPatch } from './prefs-hydrate';

type VerifiedPrefsTarget = {
	compatible_filter: CompatibleFilterPrefs;
	library_watcher_folders: string[];
	confirm: object;
};

type SyncDiskPrefs = (
	patch: DiskPrefsPatch,
	putOverride?: (patch: DiskPrefsPatch) => Promise<void>
) => Promise<void>;

export function makeVerifiedPrefWriters(deps: {
	uiPrefs: VerifiedPrefsTarget;
	persist: () => void;
	sync: SyncDiskPrefs;
	put: (patch: DiskPrefsPatch) => Promise<void>;
}) {
	const { uiPrefs, persist, sync, put } = deps;
	const confirm = () => uiPrefs.confirm as Record<string, unknown>;
	// Confirm keys set locally whose value disk has not acknowledged yet (in
	// flight or failed). Hydration keeps them, and every confirm write resends
	// them, so a failed write is retried rather than lost or overwritten.
	const unsavedConfirm = new Set<string>();

	function unsavedConfirmPatch(): Record<string, unknown> {
		const patch: Record<string, unknown> = {};
		for (const key of unsavedConfirm) if (key in confirm()) patch[key] = confirm()[key];
		return patch;
	}

	function markConfirmSaved(patch: Record<string, unknown>): void {
		for (const [key, value] of Object.entries(patch)) {
			if (value === null || confirm()[key] === value) unsavedConfirm.delete(key);
		}
	}

	/** Queue one verified step: `build` runs when the step executes. */
	function step(build: () => { patch: DiskPrefsPatch; commit: () => void }): Promise<void> {
		return sync({}, async () => {
			const { patch, commit } = build();
			await put(patch);
			commit();
			persist();
		});
	}

	return {
		/** Compatible-filter ranges: commit only after the PUT lands. */
		patchCompatibleFilter(patch: Partial<CompatibleFilterPrefs>): Promise<void> {
			return step(() => {
				const next = { ...uiPrefs.compatible_filter, ...patch };
				return {
					patch: { compatible_filter: next },
					commit: () => {
						uiPrefs.compatible_filter = next;
					}
				};
			});
		},

		/** LIBM-129 v1 placeholder: paths must already pass syntax + existence
		 * checks. Rejects on a failed PUT and keeps the previous folders. */
		setLibraryWatcherFolders(paths: readonly string[]): Promise<void> {
			const next = [...paths];
			return step(() => ({
				patch: { library_watcher_folders: next },
				commit: () => {
					uiPrefs.library_watcher_folders = next;
				}
			}));
		},

		/** Reset a remembered confirm choice to "ask": the disk key is deleted
		 * first (resending any unsaved choices with it), and the live choice is
		 * dropped only after that lands. */
		clearConfirmPref(key: string): Promise<void> {
			return step(() => {
				const patch = { ...unsavedConfirmPatch(), [key]: null };
				return {
					patch: { confirm: patch } as DiskPrefsPatch,
					commit: () => {
						delete confirm()[key];
						markConfirmSaved(patch);
					}
				};
			});
		},

		/** Remember a confirm choice: live at once (the prompt that set it has
		 * already been answered), and unsaved until the verified PUT lands. A
		 * failed PUT rejects and leaves the key unsaved for the next write. */
		setConfirmPref(key: string, value: unknown): Promise<void> {
			confirm()[key] = value;
			persist();
			unsavedConfirm.add(key);
			return sync({}, async () => {
				const patch = unsavedConfirmPatch();
				if (Object.keys(patch).length === 0) return;
				await put({ confirm: patch } as DiskPrefsPatch);
				markConfirmSaved(patch);
			});
		},

		/** True while a local confirm choice is not yet acknowledged on disk. */
		isConfirmUnsaved(key: string): boolean {
			return unsavedConfirm.has(key);
		}
	};
}
