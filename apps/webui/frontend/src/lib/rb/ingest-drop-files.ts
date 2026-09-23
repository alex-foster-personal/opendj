/**
 * Recursive folder traversal for ingest drag-and-drop.
 * Uses webkitGetAsEntry when available; falls back to flat FileList.
 */

export const AUDIO_EXT_RE = /\.(mp3|m4a|aac|wav|aiff?|flac|ogg|alac)$/i;

function isAudioName(name: string): boolean {
	return AUDIO_EXT_RE.test(name);
}

type DropFsEntry = {
	isFile: boolean;
	isDirectory: boolean;
	name: string;
	fullPath: string;
	file: (success: (f: File) => void, error?: (err: DOMException) => void) => void;
	createReader: () => { readEntries: (cb: (entries: DropFsEntry[]) => void) => void };
};

function entryFromItem(item: DataTransferItem): DropFsEntry | null {
	const fn = (item as DataTransferItem & { webkitGetAsEntry?: () => unknown }).webkitGetAsEntry;
	if (typeof fn !== 'function') return null;
	// `.call(item)`, NOT `fn()`. Detaching a DOM method from its receiver and
	// calling it bare throws `TypeError: Illegal invocation` in every browser
	// this app runs in, which is thrown out of the async drop handler as an
	// unhandled rejection: no modal, no toast, nothing in the UI at all.
	return fn.call(item) as DropFsEntry | null;
}

async function readAllEntries(reader: {
	readEntries: (cb: (entries: DropFsEntry[]) => void) => void;
}): Promise<DropFsEntry[]> {
	const out: DropFsEntry[] = [];
	for (;;) {
		const batch = await new Promise<DropFsEntry[]>((resolve) => {
			reader.readEntries(resolve);
		});
		if (batch.length === 0) break;
		out.push(...batch);
	}
	return out;
}

function fileFromEntry(entry: DropFsEntry): Promise<File> {
	return new Promise((resolve, reject) => {
		entry.file(
			(f) => {
				const rel = entry.fullPath.replace(/^\//, '');
				if (rel && rel !== f.name) {
					Object.defineProperty(f, 'name', { value: rel, configurable: true });
				}
				resolve(f);
			},
			reject
		);
	});
}

async function walkEntry(entry: DropFsEntry): Promise<File[]> {
	if (entry.isFile) {
		if (!isAudioName(entry.name)) return [];
		return [await fileFromEntry(entry)];
	}
	if (!entry.isDirectory) return [];
	const reader = entry.createReader();
	const children = await readAllEntries(reader);
	const files: File[] = [];
	for (const child of children) {
		files.push(...(await walkEntry(child)));
	}
	return files;
}

export async function collectDroppedAudioFiles(dt: DataTransfer): Promise<File[]> {
	const items = dt.items;
	if (items && items.length > 0) {
		const entries: DropFsEntry[] = [];
		for (const item of items) {
			const entry = entryFromItem(item);
			if (entry) entries.push(entry);
		}
		if (entries.length > 0) {
			const files: File[] = [];
			for (const entry of entries) {
				files.push(...(await walkEntry(entry)));
			}
			return files;
		}
	}
	return Array.from(dt.files ?? []).filter((f) => isAudioName(f.name));
}

/**
 * True for an OS file/folder drag (Finder, Explorer). The standard `Files`
 * type is exposed during dragover in every engine, WebKit included; it is
 * only CUSTOM MIME types (the in-app track drag) that WebKit hides, which is
 * why in-app TRACK drops accept on drag state instead (track-drag.svelte).
 */
export function isOsFileDrag(event: DragEvent): boolean {
	return event.dataTransfer?.types.includes('Files') === true;
}

/** First top-level directory name from a Finder folder drop, or null for flat files. */
export function collectDroppedFolderName(dt: DataTransfer): string | null {
	const items = dt.items;
	if (!items || items.length === 0) return null;
	for (const item of items) {
		const entry = entryFromItem(item);
		if (entry?.isDirectory) return entry.name;
	}
	return null;
}
