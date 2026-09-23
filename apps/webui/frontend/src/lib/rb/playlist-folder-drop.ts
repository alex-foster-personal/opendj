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

/** Mirrors the server's BATCH_RE in apps/webui/server/routes/ingest.py. */
const BATCH_RE = /^[A-Za-z0-9][A-Za-z0-9._ -]{0,79}$/;
const BATCH_MAX_LEN = 80;

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

	const batch = batchNameFromFolder(name);
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
		const skippedDup = upload.results.filter(
			(r) =>
				r.verdict === 'skipped_duplicate' ||
				(r.verdict === 'possible_duplicate' && decisions.get(r.filename) === 'reject')
		).length;
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

/** UTC stamp to the second, e.g. 20260923-141503. */
function _utcStamp(d: Date): string {
	const p = (n: number) => String(n).padStart(2, '0');
	return (
		`${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}` +
		`-${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())}`
	);
}

/** 8 hex chars of cryptographic entropy. */
function _entropy(): string {
	const bytes = new Uint8Array(4);
	crypto.getRandomValues(bytes);
	return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * A FRESH batch name for every drop: a readable folder-derived prefix plus a
 * UTC second stamp and random entropy. A batch is a persistent staging dir
 * under the ingest inbox (files stay there for the Rekordbox import), and
 * /ingest/upload answers 409 for any filename already staged in it, so a
 * batch derived from the folder name alone makes every re-drop or retry of
 * the same folder fail.
 */
export function batchNameFromFolder(folderName: string, now: Date = new Date()): string {
	const suffix = `${_utcStamp(now)}-${_entropy()}`;
	const slug = folderName
		.trim()
		.replace(/[^A-Za-z0-9._ -]+/g, '-')
		.replace(/^[^A-Za-z0-9]+/, '')
		.slice(0, BATCH_MAX_LEN - suffix.length - 1)
		.replace(/[ .-]+$/, '');
	const name = slug === '' ? `drop-${suffix}` : `${slug}-${suffix}`;
	if (!BATCH_RE.test(name)) {
		throw new Error(`generated batch name ${JSON.stringify(name)} fails BATCH_RE`);
	}
	return name;
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

function _needsMaterialize(
	row: UploadFileResult,
	decisions: Map<string, PossibleDupDecision>
): boolean {
	return (
		row.verdict === 'new' ||
		(row.verdict === 'possible_duplicate' && decisions.get(row.filename) === 'accept')
	);
}

/**
 * Playlist members in the folder walk's order (the order upload.results
 * carries): a linked duplicate resolves to its existing track, a staged file
 * to the stable_id materialize wrote for it. One ordered pass, deduplicated.
 */
async function _stableIdsFromUpload(
	upload: UploadOut,
	decisions: Map<string, PossibleDupDecision>
): Promise<string[]> {
	let byPath = new Map<string, string>();
	if (upload.results.some((r) => _needsMaterialize(r, decisions))) {
		const materialized = await materializeIngestBatch(upload.batch);
		byPath = new Map(materialized.tracks.map((t) => [t.relative_path, t.stable_id]));
	}

	const out: string[] = [];
	const seen = new Set<string>();
	for (const row of upload.results) {
		let id: string | null | undefined = null;
		if (_needsMaterialize(row, decisions)) {
			id = byPath.get(row.filename);
		} else if (
			row.verdict === 'skipped_duplicate' ||
			(row.verdict === 'possible_duplicate' && decisions.get(row.filename) === 'reject')
		) {
			id = row.duplicate_of?.stable_id;
		}
		if (!id || seen.has(id)) continue;
		seen.add(id);
		out.push(id);
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
