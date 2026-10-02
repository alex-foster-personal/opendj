/**
 * Typed HTTP client for the duplicate-review decision workflow.
 *
 * CONVERTED onto the generated OpenAPI client (`src/lib/api/client.ts`).
 * Transport only: the hand-written runtime validators below are deliberately
 * kept. Generated types are a compile-time contract, and a merge decision is
 * irreversible, so the response is still parsed field by field at runtime and
 * the ETag is still cross-checked against the body revision.
 */

import { API_BASE, ApiError, api } from '$lib/api/client';
import type { paths } from '$lib/api-types';

import type {
	ApplyRecord,
	ClustersResponse,
	Decision,
	DecisionAction,
	DecisionRecord,
	PlaylistRewrite,
	UndoRecord
} from './types';

const DECISION_ACTIONS: readonly DecisionAction[] = ['merge', 'keep-all', 'skip'];

export class DedupConflictError extends Error {
	constructor(
		message: string,
		public revision: string
	) {
		super(message);
	}
}

function asObject(value: unknown, context: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`dedup: ${context} is not an object`);
	}
	return value as Record<string, unknown>;
}

function asString(value: unknown, context: string): string {
	if (typeof value !== 'string') throw new Error(`dedup: ${context} is not a string`);
	return value;
}

function asStringOrNull(value: unknown, context: string): string | null {
	if (value === undefined) throw new Error(`dedup: ${context} is missing (expected string or null)`);
	return value === null ? null : asString(value, context);
}

function asNumberOrNull(value: unknown, context: string): number | null {
	if (value === undefined) throw new Error(`dedup: ${context} is missing (expected number or null)`);
	if (value === null) return null;
	if (typeof value !== 'number') throw new Error(`dedup: ${context} is not a number`);
	return value;
}

function asBoolean(value: unknown, context: string): boolean {
	if (typeof value !== 'boolean') throw new Error(`dedup: ${context} is not a boolean`);
	return value;
}

function asArray(value: unknown, context: string): unknown[] {
	if (!Array.isArray(value)) throw new Error(`dedup: ${context} is not an array`);
	return value;
}

function asInteger(value: unknown, context: string): number {
	if (typeof value !== 'number' || !Number.isInteger(value)) {
		throw new Error(`dedup: ${context} is not an integer`);
	}
	return value;
}

function asDecisionAction(value: unknown, context: string): DecisionAction {
	if (typeof value !== 'string' || !DECISION_ACTIONS.includes(value as DecisionAction)) {
		throw new Error(`dedup: ${context} is not a supported action`);
	}
	return value as DecisionAction;
}

function parseDecision(value: unknown, context: string): Decision | null {
	if (value === undefined) throw new Error(`dedup: ${context} is missing (expected object or null)`);
	if (value === null) return null;
	const object = asObject(value, context);
	return {
		cluster_key: asString(object.cluster_key, `${context}.cluster_key`),
		survivor: asString(object.survivor, `${context}.survivor`),
		action: asDecisionAction(object.action, `${context}.action`),
		decided_at: asString(object.decided_at, `${context}.decided_at`),
		pending_apply: asBoolean(object.pending_apply, `${context}.pending_apply`)
	};
}

function parseMember(value: unknown, context: string) {
	const object = asObject(value, context);
	return {
		stable_id: asString(object.stable_id, `${context}.stable_id`),
		path: asString(object.path, `${context}.path`),
		is_canonical: asBoolean(object.is_canonical, `${context}.is_canonical`),
		similarity: asNumberOrNull(object.similarity, `${context}.similarity`),
		title: asStringOrNull(object.title, `${context}.title`),
		artist: asStringOrNull(object.artist, `${context}.artist`),
		bpm: asNumberOrNull(object.bpm, `${context}.bpm`),
		key: asStringOrNull(object.key, `${context}.key`),
		duration_ms: asNumberOrNull(object.duration_ms, `${context}.duration_ms`),
		rating: asNumberOrNull(object.rating, `${context}.rating`),
		file_exists: asBoolean(object.file_exists, `${context}.file_exists`),
		cue_count: asInteger(object.cue_count, `${context}.cue_count`),
		hot_cue_count: asInteger(object.hot_cue_count, `${context}.hot_cue_count`),
		loop_count: asInteger(object.loop_count, `${context}.loop_count`),
		has_beatgrid: asBoolean(object.has_beatgrid, `${context}.has_beatgrid`),
		cue_positions_ms: asArray(object.cue_positions_ms, `${context}.cue_positions_ms`).map(
			(entry, index) => asInteger(entry, `${context}.cue_positions_ms[${index}]`)
		)
	};
}

function parseCluster(value: unknown, context: string) {
	const object = asObject(value, context);
	const clusterId = asInteger(object.cluster_id, `${context}.cluster_id`);
	const clusterContext = `cluster ${clusterId}`;
	return {
		cluster_id: clusterId,
		cluster_key: asString(object.cluster_key, `${clusterContext}.cluster_key`),
		survivor_stable_id: asString(
			object.survivor_stable_id,
			`${clusterContext}.survivor_stable_id`
		),
		rationale: asStringOrNull(object.rationale, `${clusterContext}.rationale`),
		flagged_manual_review: asBoolean(
			object.flagged_manual_review,
			`${clusterContext}.flagged_manual_review`
		),
		members: asArray(object.members, `${clusterContext}.members`).map((member, index) =>
			parseMember(member, `${clusterContext}.members[${index}]`)
		),
		decision: parseDecision(object.decision, `${clusterContext}.decision`)
	};
}

export function validateClustersResponse(raw: unknown): ClustersResponse {
	const object = asObject(raw, 'response');
	const clusters = asArray(object.clusters, 'clusters').map((cluster, index) =>
		parseCluster(cluster, `clusters[${index}]`)
	);
	const seen = new Set<string>();
	for (const cluster of clusters) {
		if (seen.has(cluster.cluster_key)) {
			throw new Error(`dedup: duplicate cluster_key ${cluster.cluster_key}`);
		}
		seen.add(cluster.cluster_key);
	}
	return {
		clusters,
		note: asStringOrNull(object.note, 'note'),
		revision: asString(object.revision, 'revision')
	};
}

function parseMutationRecord(raw: unknown, context: string) {
	const object = asObject(raw, context);
	return {
		cluster_id: asInteger(object.cluster_id, `${context}.cluster_id`),
		cluster_key: asString(object.cluster_key, `${context}.cluster_key`),
		survivor: asString(object.survivor, `${context}.survivor`),
		action: asDecisionAction(object.action, `${context}.action`),
		decided_at: asString(object.decided_at, `${context}.decided_at`),
		pending_apply: asBoolean(object.pending_apply, `${context}.pending_apply`),
		revision: asString(object.revision, `${context}.revision`)
	};
}

export function validateDecisionRecord(raw: unknown): DecisionRecord {
	const result = parseMutationRecord(raw, 'decision response');
	if (!result.pending_apply) {
		throw new Error('dedup: decision response must remain pending apply');
	}
	return result;
}

function parsePlaylistRewrite(value: unknown, context: string): PlaylistRewrite {
	const object = asObject(value, context);
	return {
		playlist_id: asString(object.playlist_id, `${context}.playlist_id`),
		name: asString(object.name, `${context}.name`),
		before: asArray(object.before, `${context}.before`).map((item, index) =>
			asString(item, `${context}.before[${index}]`)
		),
		after: asArray(object.after, `${context}.after`).map((item, index) =>
			asString(item, `${context}.after[${index}]`)
		)
	};
}

export function validateApplyRecord(raw: unknown): ApplyRecord {
	const object = asObject(raw, 'apply response');
	const result = parseMutationRecord(raw, 'apply response');
	if (result.action !== 'merge') {
		throw new Error('dedup: apply response.action is not merge');
	}
	return {
		...result,
		action: 'merge',
		playlists: asArray(object.playlists, 'apply response.playlists').map((row, index) =>
			parsePlaylistRewrite(row, `apply response.playlists[${index}]`)
		)
	};
}

export function validateUndoRecord(raw: unknown): UndoRecord {
	const object = asObject(raw, 'undo response');
	const result = parseMutationRecord(raw, 'undo response');
	if (result.action !== 'merge') {
		throw new Error('dedup: undo response.action is not merge');
	}
	return {
		...result,
		action: 'merge',
		playlist_ids: asArray(object.playlist_ids, 'undo response.playlist_ids').map((item, index) =>
			asString(item, `undo response.playlist_ids[${index}]`)
		)
	};
}

function requireMatchingRevision(response: Response, bodyRevision: string): void {
	const headerRevision = response.headers.get('etag');
	if (headerRevision === null) throw new Error('dedup: response is missing ETag');
	if (headerRevision !== bodyRevision) {
		throw new Error('dedup: response revision does not match ETag');
	}
}

/** Keep this module's two failure shapes after the conversion: a daemon that
 * answered (status + its own message) and a daemon that could not be reached
 * at all. An aborted request lands in the second bucket, exactly as it did
 * when this module called fetch directly. */
function asDedupError(error: unknown, route: string): Error {
	if (error instanceof ApiError) {
		return new Error(`${route} failed: ${error.status} ${error.message}`);
	}
	return new Error(`daemon unreachable (${error instanceof Error ? error.message : String(error)})`);
}

/** Progress of the on-device library fingerprint scan (GET/POST /dedup/scan). */
export type DedupScanStatus =
	paths['/api/v1/dedup/scan']['get']['responses'][200]['content']['application/json'];

export async function fetchDedupScan(signal?: AbortSignal): Promise<DedupScanStatus> {
	try {
		const { data } = await api.GET('/api/v1/dedup/scan', { signal: signal ?? null });
		return data as DedupScanStatus;
	} catch (error) {
		throw asDedupError(error, 'GET /api/v1/dedup/scan');
	}
}

/** Start the scan. A scan already running is not an error: its status comes back. */
export async function startDedupScan(signal?: AbortSignal): Promise<DedupScanStatus> {
	try {
		const { data } = await api.POST('/api/v1/dedup/scan', { signal: signal ?? null });
		return data as DedupScanStatus;
	} catch (error) {
		if (error instanceof ApiError && error.status === 409) return fetchDedupScan(signal);
		throw asDedupError(error, 'POST /api/v1/dedup/scan');
	}
}

export async function fetchDedupClusters(signal?: AbortSignal): Promise<ClustersResponse> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.GET('/api/v1/dedup/clusters', { signal: signal ?? null }));
	} catch (error) {
		throw asDedupError(error, 'GET /api/v1/dedup/clusters');
	}
	const result = validateClustersResponse(data);
	requireMatchingRevision(response, result.revision);
	return result;
}

export async function postDedupDecision(
	clusterId: number,
	clusterKey: string,
	survivor: string,
	action: DecisionAction,
	revision: string,
	signal?: AbortSignal
): Promise<DecisionRecord> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.POST('/api/v1/dedup/clusters/{cluster_id}/decision', {
			params: { path: { cluster_id: clusterId }, header: { 'If-Match': revision } },
			body: { cluster_key: clusterKey, survivor, action },
			signal: signal ?? null
		}));
	} catch (error) {
		// 409 is the CAS outcome, not a fault: the caller refreshes and retries
		// against the revision the daemon reports in its ETag.
		if (error instanceof ApiError && error.status === 409) {
			throw new DedupConflictError(
				'duplicate review changed; refresh before retrying',
				error.response.headers.get('etag') ?? ''
			);
		}
		throw asDedupError(error, `POST /api/v1/dedup/clusters/${clusterId}/decision`);
	}
	const result = validateDecisionRecord(data);
	requireMatchingRevision(response, result.revision);
	return result;
}

function asDedupConflict(error: unknown, route: string): Error {
	if (error instanceof ApiError && error.status === 409) {
		return new DedupConflictError(
			'duplicate review changed; refresh before retrying',
			error.response.headers.get('etag') ?? ''
		);
	}
	return asDedupError(error, route);
}

export async function applyDedupMerge(
	clusterId: number,
	clusterKey: string,
	survivor: string,
	revision: string,
	signal?: AbortSignal
): Promise<ApplyRecord> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.POST('/api/v1/dedup/clusters/{cluster_id}/apply', {
			params: { path: { cluster_id: clusterId }, header: { 'If-Match': revision } },
			body: { cluster_key: clusterKey, survivor, confirm_cue_loss: false },
			signal: signal ?? null
		}));
	} catch (error) {
		throw asDedupConflict(error, `POST /api/v1/dedup/clusters/${clusterId}/apply`);
	}
	const result = validateApplyRecord(data);
	requireMatchingRevision(response, result.revision);
	return result;
}

export async function undoDedupMerge(
	clusterId: number,
	clusterKey: string,
	survivor: string,
	revision: string,
	signal?: AbortSignal
): Promise<UndoRecord> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.POST('/api/v1/dedup/clusters/{cluster_id}/undo', {
			params: { path: { cluster_id: clusterId }, header: { 'If-Match': revision } },
			body: { cluster_key: clusterKey, survivor, confirm_cue_loss: false },
			signal: signal ?? null
		}));
	} catch (error) {
		throw asDedupConflict(error, `POST /api/v1/dedup/clusters/${clusterId}/undo`);
	}
	const result = validateUndoRecord(data);
	requireMatchingRevision(response, result.revision);
	return result;
}

export function dedupArtworkUrl(stableId: string): string {
	return `${API_BASE}/api/v1/tracks/${encodeURIComponent(stableId)}/artwork?size=s`;
}
