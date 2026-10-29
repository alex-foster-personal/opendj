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
import { validateLibraryKeyFilterField } from './library-key-filter';

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
	library_key_filter?: string[];
};

type VerifiedPrefsTarget = {
	compatible_filter: CompatibleFilterPrefs;
	library_watcher_folders: string[];
	library_key_filter: string[];
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
	 * flight or failed), each with the revision of its latest local write.
	 * Owned by prefs.svelte.ts so hydration can read it before this module
	 * loads; every confirm write here resends them. */
	unsavedConfirm: Map<string, number>;
	/** Non-zero while a local library_key_filter write is not yet on disk. */
	libraryKeyFilterUnsaved: { rev: number };
	markLibraryKeyFilterSaved: (writeRev: number) => void;
}) {
	const { uiPrefs, persist, sync, put, unsavedConfirm, markLibraryKeyFilterSaved } = deps;
	const confirm = () => uiPrefs.confirm as Record<string, unknown>;

	/** The unsaved choices to send, with the revisions they were taken at. */
	function unsavedConfirmPatch() {
		const patch: Record<string, unknown> = {};
		for (const key of unsavedConfirm.keys()) if (key in confirm()) patch[key] = confirm()[key];
		return { patch, revs: new Map<string, number | undefined>(unsavedConfirm) };
	}

	/** A key is saved only if no newer local write replaced it meanwhile. */
	function markConfirmSaved(patch: Record<string, unknown>, revs: Map<string, number | undefined>): void {
		for (const key of Object.keys(patch)) {
			if (unsavedConfirm.get(key) === revs.get(key)) unsavedConfirm.delete(key);
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

		/** Library key wheel selection: PUT the live array when this step runs.
		 * The setter already installed the selection locally, so the commit only
		 * marks it saved: assigning this step's snapshot back would revert a
		 * click made while the PUT was in flight (Codex P2, #7743). */
		setLibraryKeyFilterDisk(writeRev: number): Promise<void> {
			return step(() => ({
				patch: { library_key_filter: [...uiPrefs.library_key_filter] },
				commit: () => markLibraryKeyFilterSaved(writeRev)
			}));
		},

		/** Reset a remembered confirm choice to "ask": the disk key is deleted
		 * first (resending any unsaved choices with it), and the live choice is
		 * dropped only after that lands. `rev` is the key's unsaved revision when
		 * the reset was asked for: a choice made after that is newer than the
		 * reset, so it is kept and its own queued save sends it (Sol P2, #4014). */
		clearConfirmPref(key: string, rev: number | undefined): Promise<void> {
			return step(() => {
				const { patch, revs } = unsavedConfirmPatch();
				patch[key] = null;
				revs.set(key, rev);
				return {
					patch: { confirm: patch } as DiskPrefsPatch,
					commit: () => {
						if (unsavedConfirm.get(key) === rev) delete confirm()[key];
						markConfirmSaved(patch, revs);
					}
				};
			});
		},

		/** Send every unsaved confirm choice with the verified PUT. A failed PUT
		 * rejects and leaves the keys unsaved for the next write. */
		saveUnsavedConfirm(): Promise<void> {
			return sync({}, async () => {
				const { patch, revs } = unsavedConfirmPatch();
				if (Object.keys(patch).length === 0) return;
				await put({ confirm: patch } as DiskPrefsPatch);
				markConfirmSaved(patch, revs);
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

/** Adopt the disk selection unless a local write is still unsaved. A
 * missing or malformed value throws: it is contract drift, and keeping the old
 * selection quietly would hide it (Sol P1, #7743). */
export function hydrateLibraryKeyFilter(
	uiPrefs: { library_key_filter: string[] },
	body: { library_key_filter?: unknown },
	isUnsaved: () => boolean
): void {
	// The server always sends the field (UiPrefsOut default []), like key_notation and
	// library_hidden_columns, so an absent one is contract drift (Sol P1, #7743).
	if (body.library_key_filter === undefined) {
		throw new Error('GET /api/v1/ui-prefs: library_key_filter is missing from the response');
	}
	const next = validateLibraryKeyFilterField(body.library_key_filter, 'GET /api/v1/ui-prefs');
	if (isUnsaved()) return;
	uiPrefs.library_key_filter = next;
}
