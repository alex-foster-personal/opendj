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
 *
 * Loaded on first use and alongside the boot GET, never at first paint, so the
 * matching hydration for confirm and compatible-filter prefs lives here too
 * (library bundle budget).
 */
import {
	COMPATIBLE_FILTER_DEFAULTS,
	validateCompatibleFilterPrefs,
	type CompatibleFilterPrefs
} from './compatible-filter-prefs';

/** Disk/wire confirm patch: null deletes a key; values are per-key typed.
 * Defined here, not in prefs-hydrate.ts, so this lazily loaded module never
 * imports its importer back (frontend.import_cycles). */
export type DiskConfirmPatch = {
	delete_playlist?: boolean | null;
	playlist_drop_mode?: 'add' | 'move' | null;
	dblclick_load_play?: boolean | null;
};

/** The disk-prefs fields these writers send (prefs-hydrate's DiskPrefsPatch is a superset). */
type DiskPrefsPatch = {
	confirm?: DiskConfirmPatch;
	compatible_filter?: CompatibleFilterPrefs;
	library_watcher_folders?: string[];
};

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
	/** Confirm keys set locally whose value disk has not acknowledged yet (in
	 * flight or failed). Owned by prefs.svelte.ts so hydration can read it
	 * before this module loads; every confirm write here resends them. */
	unsavedConfirm: Set<string>;
}) {
	const { uiPrefs, persist, sync, put, unsavedConfirm } = deps;
	const confirm = () => uiPrefs.confirm as Record<string, unknown>;

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

		/** Send every unsaved confirm choice with the verified PUT. A failed PUT
		 * rejects and leaves the keys unsaved for the next write. */
		saveUnsavedConfirm(): Promise<void> {
			return sync({}, async () => {
				const patch = unsavedConfirmPatch();
				if (Object.keys(patch).length === 0) return;
				await put({ confirm: patch } as DiskPrefsPatch);
				markConfirmSaved(patch);
			});
		}
	};
}

const KNOWN_CONFIRM_KEYS = ['delete_playlist', 'playlist_drop_mode', 'dblclick_load_play'];

/** A successful read is authoritative for the known confirm keys: one absent
 * from the disk map is "ask" and is removed locally, so a reset made in another
 * browser profile reaches this one (PR #4014, Sol). A key whose local write is
 * not yet acknowledged on disk is kept, and the next write resends it. */
export function hydrateConfirmFromDisk(
	uiPrefs: { confirm: object },
	diskConfirm: DiskConfirmPatch,
	isUnsaved: (key: string) => boolean
): void {
	const next: Record<string, unknown> = { ...uiPrefs.confirm };
	for (const key of KNOWN_CONFIRM_KEYS) {
		if (!(key in diskConfirm) && !isUnsaved(key)) delete next[key];
	}
	for (const [key, value] of Object.entries(diskConfirm)) {
		if (isUnsaved(key)) continue;
		// null deletes; drop mode takes 'add'|'move'; every other key takes a boolean.
		if (value === null) delete next[key];
		else if (key === 'playlist_drop_mode' ? value === 'add' || value === 'move' : typeof value === 'boolean')
			next[key] = value;
	}
	uiPrefs.confirm = next;
}

/** Compatible-filter ranges from GET /api/v1/ui-prefs (LIBUX-32): the disk copy
 * wins over localStorage, so a fresh browser profile gets the saved ranges. An
 * invalid object is reported and skipped rather than aborting the hydrate. */
export function hydrateCompatibleFilter(
	uiPrefs: { compatible_filter: CompatibleFilterPrefs },
	body: { compatible_filter?: unknown }
): void {
	if (body.compatible_filter === undefined || body.compatible_filter === null) return;
	try {
		uiPrefs.compatible_filter = {
			...COMPATIBLE_FILTER_DEFAULTS,
			...validateCompatibleFilterPrefs(body.compatible_filter, 'GET /api/v1/ui-prefs')
		};
	} catch (exc) {
		console.error('[ui-prefs] compatible_filter from disk rejected', exc);
	}
}
