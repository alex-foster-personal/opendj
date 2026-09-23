/**
 * Pure rules for the first-run folder-import rows: which rows import, and why
 * Next refuses on the folder branch of the detect step.
 *
 * Split out of wizard.svelte.ts so the rune module stays under the frontend
 * file-size limit. No $state here; the wizard store owns the rows and passes
 * them in, and wizard.svelte.ts re-exports these names so importers keep one
 * entry point.
 */

import { folderIsImportable, normalizeSetupFolderPath, type FolderScan } from './setup-api';

/** One folder path row on the first-run folder-import step. */
export type FolderRow = {
	id: string;
	path: string;
	scan: FolderScan | null;
};

export function newFolderRow(): FolderRow {
	return { id: crypto.randomUUID(), path: '', scan: null };
}

/**
 * The row's scan, but only while it still describes the path in the text box.
 *
 * `bind:value` edits `row.path` in place and leaves `row.scan` alone, so a row
 * checked as /Music/A and then retyped as /Music/B would otherwise import B on
 * the strength of A's scan. The server echoes the path it scanned, normalized
 * the same way (`normalize_setup_folder_path`), so a mismatch means stale.
 */
export function currentFolderScan(row: FolderRow): FolderScan | null {
	if (row.scan === null) return null;
	return normalizeSetupFolderPath(row.path) === row.scan.path ? row.scan : null;
}

/** Non-empty folder rows that passed check and are importable, normalized and deduped. */
export function importableFolderPathsFromRows(rows: FolderRow[]): string[] {
	const paths: string[] = [];
	const seen = new Set<string>();
	for (const row of rows) {
		if (row.path.trim() === '' || !folderIsImportable(currentFolderScan(row))) continue;
		const canon = normalizeSetupFolderPath(row.path);
		if (seen.has(canon)) continue;
		seen.add(canon);
		paths.push(canon);
	}
	return paths;
}

/** Why Next is refused on the detect step's folder branch, or null when allowed. */
export function folderAdvanceRefusal(rows: FolderRow[]): string | null {
	const nonEmpty = rows.filter((row) => row.path.trim() !== '');
	const anyChecked = rows.some((row) => currentFolderScan(row) !== null);
	if (!anyChecked) return 'no folder has been checked yet';

	for (const row of nonEmpty) {
		const scan = currentFolderScan(row);
		if (scan === null) return `check ${row.path.trim()} first`;
		if (scan.denied) {
			return 'macOS is blocking that folder; grant access and check again';
		}
		if (!folderIsImportable(scan)) {
			return `nothing importable in ${scan.path}`;
		}
	}

	const importable = importableFolderPathsFromRows(rows);
	if (importable.length === 0) return 'no folder has been checked yet';

	const normalized = nonEmpty
		.filter((row) => folderIsImportable(currentFolderScan(row)))
		.map((row) => normalizeSetupFolderPath(row.path));
	if (new Set(normalized).size !== normalized.length) {
		return 'remove duplicate folder paths before importing';
	}
	return null;
}
