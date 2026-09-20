/**
 * Orchestrate playlist-tree folder drop: create playlist, upload, materialize,
 * link duplicates, add membership (issue #3182 / LIBUX-16).
 */
import {
	decideIngestUpload,
	materializeIngestBatch,
	startIngestRefresh,
	uploadIngestFiles,
	type UploadFileResult,
	type UploadOut
} from '$lib/rb/api-ingest';
import { refreshIngestPending } from '$lib/rb/ingest-pending.svelte';
import {
	addPlaylistItems,
	createPlaylist,
	deletePlaylist,
	getPlaylistTracksEtag
} from '$lib/rb/playlist-write';

const BATCH_RE = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$/;

export type FolderDropResult = {
	playlistId: string;
	added: number;
	staged: number;
	skippedDup: number;
};

export type PossibleDupDecision = 'accept' | 'reject';

export async function ingestFolderToNewPlaylist(opts: {
	files: File[];
	folderName: string;
	onPossibleDups?: (
		rows: UploadFileResult[]
	) => Promise<Map<string, PossibleDupDecision> | null>;
}): Promise<FolderDropResult> {
	const name = opts.folderName.trim();
	if (name === '') {
		throw new Error('playlist name is empty');
	}

	const batch = _batchNameFromFolder(name);
	let playlistId: string | null = null;

	try {
		const created = await createPlaylist(name);
		playlistId = created.playlist_id;

		const upload = await uploadIngestFiles(opts.files, batch);
		const decisions = await _resolvePossibleDups(upload, opts.onPossibleDups);
		const stableIds = await _stableIdsFromUpload(upload, decisions);
		if (stableIds.length > 0) {
			await addPlaylistItems(playlistId, stableIds);
		}

		const staged = upload.results.filter(
			(r) =>
				r.verdict === 'new' ||
				(r.verdict === 'possible_duplicate' && decisions.get(r.filename) === 'accept')
		).length;
		const skippedDup = upload.results.filter((r) => r.verdict === 'skipped_duplicate').length;
		if (staged > 0) {
			await startIngestRefresh(upload.dest_dir);
		}
		await refreshIngestPending();

		return {
			playlistId,
			added: stableIds.length,
			staged,
			skippedDup
		};
	} catch (err) {
		if (playlistId !== null) {
			await _deleteIfEmpty(playlistId);
		}
		throw err;
	}
}

function _defaultBatch(): string {
	const d = new Date();
	const p = (n: number) => String(n).padStart(2, '0');
	return `drop-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

export function batchNameFromFolder(folderName: string): string {
	const trimmed = folderName.trim();
	const slug = trimmed
		.replace(/[^A-Za-z0-9._ -]+/g, '-')
		.replace(/^-+/, '')
		.replace(/-+$/, '');
	if (slug !== '' && BATCH_RE.test(slug)) return slug;
	return _defaultBatch();
}

function _batchNameFromFolder(folderName: string): string {
	return batchNameFromFolder(folderName);
}

async function _resolvePossibleDups(
	upload: UploadOut,
	onPossibleDups?: (
		rows: UploadFileResult[]
	) => Promise<Map<string, PossibleDupDecision> | null>
): Promise<Map<string, PossibleDupDecision>> {
	const held = upload.results.filter((r) => r.verdict === 'possible_duplicate');
	if (held.length === 0) return new Map();

	if (onPossibleDups === undefined) {
		const decisions = new Map<string, PossibleDupDecision>();
		for (const row of held) {
			decisions.set(row.filename, 'reject');
			await decideIngestUpload({
				batch: upload.batch,
				filename: row.filename,
				action: 'reject'
			});
		}
		return decisions;
	}

	const chosen = await onPossibleDups(held);
	if (chosen === null) return new Map();

	const decisions = new Map<string, PossibleDupDecision>();
	for (const row of held) {
		const action = chosen.get(row.filename) ?? 'reject';
		decisions.set(row.filename, action);
		await decideIngestUpload({
			batch: upload.batch,
			filename: row.filename,
			action: action === 'accept' ? 'accept' : 'reject'
		});
	}
	return decisions;
}

async function _stableIdsFromUpload(
	upload: UploadOut,
	decisions: Map<string, PossibleDupDecision>
): Promise<string[]> {
	const out: string[] = [];
	const seen = new Set<string>();
	const possibleByName = new Map(
		upload.results
			.filter((r) => r.verdict === 'possible_duplicate')
			.map((r) => [r.filename, r] as const)
	);

	const add = (id: string | undefined | null): void => {
		if (!id || seen.has(id)) return;
		seen.add(id);
		out.push(id);
	};

	for (const row of upload.results) {
		if (row.verdict === 'skipped_duplicate') {
			add(row.duplicate_of?.stable_id ?? null);
		}
	}

	for (const [filename, action] of decisions) {
		const original = possibleByName.get(filename);
		if (action === 'reject') {
			add(original?.duplicate_of?.stable_id ?? null);
		}
	}

	const needsMaterialize = upload.results.some(
		(r) =>
			r.verdict === 'new' ||
			(r.verdict === 'possible_duplicate' && decisions.get(r.filename) === 'accept')
	);
	if (!needsMaterialize) return out;

	const materialized = await materializeIngestBatch(upload.batch);
	const byPath = new Map(materialized.tracks.map((t) => [t.relative_path, t.stable_id]));

	for (const row of upload.results) {
		if (row.verdict === 'new') {
			add(byPath.get(row.filename) ?? null);
			continue;
		}
		if (row.verdict === 'possible_duplicate' && decisions.get(row.filename) === 'accept') {
			add(byPath.get(row.filename) ?? null);
		}
	}
	return out;
}

async function _deleteIfEmpty(playlistId: string): Promise<void> {
	try {
		const { detail, etag } = await getPlaylistTracksEtag(playlistId);
		if (detail.tracks.length === 0) {
			await deletePlaylist(playlistId, etag);
		}
	} catch {
		// Leave partial playlist; caller surfaces the error.
	}
}
