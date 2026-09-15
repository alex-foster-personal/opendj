/**
 * Typed client for the CloudSync config router
 * (LANE config-ui -- apps/webui/server/routes/cloudsync.py, D5).
 *
 * `app.py` (the router-include hotspot) is owned by the sync-engine lane and
 * has not wired `cloudsync.router` in yet, so `/api/v1/cloudsync/*` is not
 * in the daemon's OpenAPI document and therefore not in the generated
 * `paths` type (`src/lib/api-types.ts`). This module declares its own path
 * table and views the ONE shared `api` client instance through it -- same
 * pattern, same rationale, as `routes/progress-tree/progress-api.ts`'s
 * `LedgerPaths` (see that file's docstring). Delete the local `CloudSyncPaths`
 * declaration once `pnpm api:gen` picks these routes up from a live daemon
 * that includes them.
 *
 * Deliberately its OWN module, not `lib/rb/api-rb.ts` (hotspot, not touched
 * by this lane) or `lib/rb/types.ts` (same).
 */

import type { Client } from 'openapi-fetch';

import { ApiError, api, unwrap } from './api/client';

// ----------------------------------------------------------- types

export const ASSET_KINDS = [
	'audio',
	'stem_bundle',
	'anlz_cache',
	'vocal_cache',
	'lyrics_cache',
	'karaoke_words'
] as const;
export type AssetKind = (typeof ASSET_KINDS)[number];

export const SYNC_MODES = ['pinned', 'cached', 'stream', 'excluded'] as const;
export type SyncMode = (typeof SYNC_MODES)[number];

export interface CloudMachine {
	machine_id: string;
	name: string;
	platform: 'macos' | 'windows' | 'linux';
	is_hub: boolean;
	data_root: string | null;
	first_seen: string;
	last_seen: string;
}

export interface SyncPolicy {
	machine_id: string;
	asset_kind: AssetKind;
	mode: SyncMode;
	cache_budget_mb: number | null;
	updated_at: string | null;
	origin_device_id: string | null;
}

export interface SyncPolicyPutBody {
	machine_id: string;
	asset_kind: AssetKind;
	mode: SyncMode;
	cache_budget_mb?: number | null;
}

export interface PlaylistPin {
	machine_id: string;
	playlist_id: string;
	playlist_name: string | null;
	mode: SyncMode;
	updated_at: string | null;
	origin_device_id: string | null;
}

export interface PlaylistPinPutBody {
	machine_id: string;
	playlist_id: string;
	mode: SyncMode;
}

export interface MachineOverview {
	machine_id: string;
	name: string;
	pinned_tracks: number;
	cached_tracks: number;
	stream_tracks: number;
	unhydrated_pinned_count: number;
	last_sync_at: string | null;
}

export interface CloudSyncOverview {
	total_tracks: number;
	machines: MachineOverview[];
}

/**
 * Three verdicts, not two. `inconclusive` is a sync that COMPLETED while its
 * post-sync digest compare excluded rows on one side, so agreement was never
 * verified. Rendering it as `ok` would be a failed measurement shown as a
 * clean result; rendering it as `error` would claim a failure that did not
 * happen. See apps/sync_hub/status.py ResultStatus.
 */
export type CloudSyncResultStatus = 'ok' | 'error' | 'inconclusive';

export interface CloudSyncRecentResult {
	finished_at: string;
	status: CloudSyncResultStatus;
	message: string;
	pushed: number;
	pulled: number;
}

/** Which source decided a CloudSync config field; an env override wins over the file. */
export type CloudSyncConfigSource = 'env' | 'file' | 'default';

export interface CloudSyncUpdateRequired {
	code: 'SYNC_WIRE_VERSION';
	local_wire_version: number;
	peer_wire_version: number;
	action: string;
}

export interface CloudSyncStatus {
	/** configured AND running: true only while a scheduler heartbeat is fresh. */
	enabled: boolean;
	/** The effective config is on and names a hub (intent, not evidence). */
	configured: boolean;
	/** A scheduler heartbeat is fresh (evidence a loop is alive). */
	running: boolean;
	heartbeat_at: string | null;
	enabled_source: CloudSyncConfigSource;
	endpoint_source: CloudSyncConfigSource;
	reason: string | null;
	signed_in_as: string | null;
	last_push_at: string | null;
	last_pull_at: string | null;
	last_result: { status: CloudSyncResultStatus; message: string } | null;
	rows_pending: number | null;
	/** Live tracks offered as hash_pending while awaiting content_hash (ADR-0047). */
	hash_pending: number | null;
	/** Direct stamp faults and identity-dup losers only (CSSTATUS-07). */
	quarantined: number | null;
	/** Every row held outside the sync set, including transitive holds. */
	excluded_total: number | null;
	endpoint: string | null;
	recent_results: CloudSyncRecentResult[];
	update_required: CloudSyncUpdateRequired | null;
}

/**
 * Identity backlog counters from GET /api/v1/cloudsync/identity-backlog.
 * `hash_pending` rows travel to the hub and sync; `unsyncable_inferred` is
 * identity-dup losers only (CLOUDSYNC-16, ADR-0047).
 */
export interface CloudSyncIdentityBacklog {
	unsyncable_inferred: number;
	hash_pending: number;
}

// ----------------------------------------------------------- local path table

type CloudSyncPaths = {
	'/api/v1/cloudsync/machines': {
		get: { responses: { 200: { content: { 'application/json': CloudMachine[] } } } };
	};
	'/api/v1/cloudsync/policies': {
		get: {
			parameters: { query?: { machine_id?: string } };
			responses: { 200: { content: { 'application/json': SyncPolicy[] } } };
		};
		put: {
			requestBody: { content: { 'application/json': SyncPolicyPutBody } };
			responses: { 200: { content: { 'application/json': SyncPolicy } } };
		};
	};
	'/api/v1/cloudsync/playlist-pins': {
		get: {
			parameters: { query?: { machine_id?: string } };
			responses: { 200: { content: { 'application/json': PlaylistPin[] } } };
		};
		put: {
			requestBody: { content: { 'application/json': PlaylistPinPutBody } };
			responses: { 200: { content: { 'application/json': PlaylistPin } } };
		};
	};
	'/api/v1/cloudsync/overview': {
		get: { responses: { 200: { content: { 'application/json': CloudSyncOverview } } } };
	};
	'/api/v1/cloudsync/status': {
		get: { responses: { 200: { content: { 'application/json': CloudSyncStatus } } } };
	};
	'/api/v1/cloudsync/identity-backlog': {
		get: { responses: { 200: { content: { 'application/json': CloudSyncIdentityBacklog } } } };
	};
};

const cloudSyncApi = api as unknown as Client<CloudSyncPaths>;

// ----------------------------------------------------------- fetchers

export async function listMachines(): Promise<CloudMachine[]> {
	return unwrap(cloudSyncApi.GET('/api/v1/cloudsync/machines', {}));
}

export async function listPolicies(machineId?: string): Promise<SyncPolicy[]> {
	return unwrap(
		cloudSyncApi.GET('/api/v1/cloudsync/policies', {
			params: { query: machineId ? { machine_id: machineId } : {} }
		})
	);
}

export async function putPolicy(body: SyncPolicyPutBody): Promise<SyncPolicy> {
	return unwrap(cloudSyncApi.PUT('/api/v1/cloudsync/policies', { body }));
}

/** One gate verdict from a 409 POLICY_VIOLATION body (`detail.outcome.violations`). */
interface PolicyViolation {
	subject: string;
	message: string;
	blocking: boolean;
}

/** A toast-ready message for a failed policy write. A 409 POLICY_VIOLATION
 * names each blocking violation (`subject: message`), so the user sees WHY the
 * gate refused; any other failure keeps the API's own message. */
export function policyErrorMessage(exc: unknown): string {
	if (!(exc instanceof ApiError)) return exc instanceof Error ? exc.message : String(exc);
	const body = exc.body as { detail?: { outcome?: { violations?: PolicyViolation[] } } } | null;
	const blocking = (body?.detail?.outcome?.violations ?? []).filter((v) => v.blocking);
	if (exc.code !== 'POLICY_VIOLATION' || blocking.length === 0) return exc.message;
	return blocking.map((v) => `${v.subject}: ${v.message}`).join('; ');
}

export async function listPlaylistPins(machineId?: string): Promise<PlaylistPin[]> {
	return unwrap(
		cloudSyncApi.GET('/api/v1/cloudsync/playlist-pins', {
			params: { query: machineId ? { machine_id: machineId } : {} }
		})
	);
}

export async function putPlaylistPin(body: PlaylistPinPutBody): Promise<PlaylistPin> {
	return unwrap(cloudSyncApi.PUT('/api/v1/cloudsync/playlist-pins', { body }));
}

export async function getOverview(): Promise<CloudSyncOverview> {
	return unwrap(cloudSyncApi.GET('/api/v1/cloudsync/overview', {}));
}

export async function getStatus(): Promise<CloudSyncStatus> {
	return unwrap(cloudSyncApi.GET('/api/v1/cloudsync/status', {}));
}

export async function getIdentityBacklog(): Promise<CloudSyncIdentityBacklog> {
	return unwrap(cloudSyncApi.GET('/api/v1/cloudsync/identity-backlog', {}));
}
