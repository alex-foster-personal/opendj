/**
 * Cloud offload client (OFFLOAD, spec (private path)).
 *
 * One function per `/api/v1/offload` route, the same routes the `opendj
 * offload` CLI and the `offload_*` MCP tools call. Every call either returns
 * the engine's document or throws its error; nothing is retried or guessed.
 */
import { api, unwrap } from '../api/client';

export type RestoreMode = 'temporary' | 'permanent';
export type OffloadState = 'local' | 'uploading' | 'offloaded' | 'restoring' | 'temporary';
export type OperationState = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

export interface OffloadTrackResult {
	state: OffloadState | null;
	note: string | null;
	error: string | null;
	message?: string;
}

/** `Operation.wire()` in apps/webui/server/cloud_offload_service.py. */
export interface OffloadOperation {
	op_id: string;
	kind: 'offload' | 'restore' | 'sweep';
	mode: RestoreMode | null;
	playlist_id: string | null;
	state: OperationState;
	stable_ids: string[];
	tracks_total: number;
	tracks_done: number;
	bytes_total: number;
	bytes_done: number;
	bytes_freed: number;
	current: string | null;
	results: Record<string, OffloadTrackResult>;
	error: string | null;
	dry_run: boolean;
}

const asOperation = (body: unknown): OffloadOperation => body as OffloadOperation;

export async function offloadTracks(stableIds: string[]): Promise<OffloadOperation> {
	return asOperation(
		await unwrap(api.POST('/api/v1/offload/tracks', { body: { stable_ids: stableIds } }))
	);
}

export async function offloadPlaylist(playlistId: string): Promise<OffloadOperation> {
	return asOperation(
		await unwrap(
			api.POST('/api/v1/offload/playlists/{playlist_id}', {
				params: { path: { playlist_id: playlistId } }
			})
		)
	);
}

export async function restoreTracks(
	stableIds: string[],
	mode: RestoreMode
): Promise<OffloadOperation> {
	return asOperation(
		await unwrap(api.POST('/api/v1/offload/restore', { body: { stable_ids: stableIds, mode } }))
	);
}

export async function restorePlaylist(
	playlistId: string,
	mode: RestoreMode
): Promise<OffloadOperation> {
	return asOperation(
		await unwrap(
			api.POST('/api/v1/offload/playlists/{playlist_id}/restore', {
				params: { path: { playlist_id: playlistId } },
				body: { mode }
			})
		)
	);
}

/** Per-track offload state (null: never offloaded here), the `opendj offload status` twin. */
export async function getTrackOffloadStates(
	stableIds: string[]
): Promise<Record<string, { state: OffloadState; expires_at: string | null } | null>> {
	const body = await unwrap(
		api.GET('/api/v1/offload/tracks', { params: { query: { stable_ids: stableIds.join(',') } } })
	);
	return body as Record<string, { state: OffloadState; expires_at: string | null } | null>;
}

export async function getOperation(opId: string): Promise<OffloadOperation> {
	return asOperation(
		await unwrap(
			api.GET('/api/v1/offload/operations/{op_id}', { params: { path: { op_id: opId } } })
		)
	);
}

export async function cancelOperation(opId: string): Promise<OffloadOperation> {
	return asOperation(
		await unwrap(
			api.POST('/api/v1/offload/operations/{op_id}/cancel', { params: { path: { op_id: opId } } })
		)
	);
}

export function isFinished(op: OffloadOperation): boolean {
	return op.state === 'done' || op.state === 'failed' || op.state === 'cancelled';
}

/** Percent of bytes moved, or null while the total is still unknown (0). */
export function operationPercent(op: OffloadOperation): number | null {
	if (op.bytes_total <= 0) return isFinished(op) ? 100 : null;
	return Math.min(100, Math.max(0, (op.bytes_done / op.bytes_total) * 100));
}

const GB = 1024 ** 3;
const MB = 1024 ** 2;

function size(bytes: number): string {
	return bytes >= GB ? `${(bytes / GB).toFixed(2)} GB` : `${(bytes / MB).toFixed(1)} MB`;
}

/** "Offloading 3 of 12 tracks, 1.20 of 4.80 GB" (the Spotify playlist-download header line). */
export function operationLabel(op: OffloadOperation): string {
	const verb =
		op.kind === 'offload' ? 'Offloading' : op.kind === 'restore' ? 'Downloading' : 'Sweeping';
	const done =
		op.state === 'done'
			? 'Done'
			: op.state === 'cancelled'
				? 'Cancelled'
				: op.state === 'failed'
					? 'Failed'
					: verb;
	return `${done}: ${op.tracks_done} of ${op.tracks_total} tracks, ${size(op.bytes_done)} of ${size(op.bytes_total)}`;
}

/** Per-track errors, worded for a toast. */
export function operationErrors(op: OffloadOperation): string[] {
	return Object.entries(op.results)
		.filter(([, result]) => result.error !== null)
		.map(([sid, result]) => `${sid.slice(0, 8)}: ${result.message ?? result.error}`);
}
