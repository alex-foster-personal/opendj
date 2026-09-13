/**
 * Recursive folder traversal for ingest drag-and-drop.
 * Uses webkitGetAsEntry when available; falls back to flat FileList.
 */

export const AUDIO_EXT_RE = /\.(mp3|m4a|aac|wav|aiff?|flac|ogg|alac)$/i;

function isAudioName(name: string): boolean {
	return AUDIO_EXT_RE.test(name);
}

type FileSystemEntry = {
	isFile: boolean;
	isDirectory: boolean;
	name: string;
	fullPath: string;
	file: (cb: (f: File) => void) => void;
	createReader: () => { readEntries: (cb: (entries: FileSystemEntry[]) => void) => void };
};

function entryFromItem(item: DataTransferItem): FileSystemEntry | null {
	const fn = (item as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntry | null })
		.webkitGetAsEntry;
	if (typeof fn !== 'function') return null;
	return fn();
}

async function readAllEntries(reader: {
	readEntries: (cb: (entries: FileSystemEntry[]) => void) => void;
}): Promise<FileSystemEntry[]> {
	const out: FileSystemEntry[] = [];
	for (;;) {
		const batch = await new Promise<FileSystemEntry[]>((resolve) => {
			reader.readEntries(resolve);
		});
		if (batch.length === 0) break;
		out.push(...batch);
	}
	return out;
}

function fileFromEntry(entry: FileSystemEntry): Promise<File> {
	return new Promise((resolve, reject) => {
		entry.file((f) => {
			const rel = entry.fullPath.replace(/^\//, '');
			if (rel && rel !== f.name) {
				Object.defineProperty(f, 'name', { value: rel, configurable: true });
			}
			resolve(f);
		}, reject);
	});
}

async function walkEntry(entry: FileSystemEntry): Promise<File[]> {
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
		const entries: FileSystemEntry[] = [];
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
