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

import { api, unwrap } from './api/client';

// ----------------------------------------------------------- types

export const ASSET_KINDS = ['audio', 'stem_bundle', 'anlz_cache', 'vocal_cache'] as const;
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

export interface CloudSyncStatus {
	enabled: boolean;
	reason: string | null;
	signed_in_as: string | null;
	last_push_at: string | null;
	last_pull_at: string | null;
	last_result: { status: CloudSyncResultStatus; message: string } | null;
	rows_pending: number | null;
	endpoint: string | null;
	recent_results: CloudSyncRecentResult[];
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
