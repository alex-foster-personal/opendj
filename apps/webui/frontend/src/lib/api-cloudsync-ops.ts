/**
 * Typed client for the CloudSync config and operator routes (plan W1 + W4):
 *
 *     GET/PUT /api/v1/cloudsync/config   (apps/webui/server/routes/cloudsync_config.py)
 *     POST    /api/v1/cloudsync/sync     (apps/webui/server/routes/cloudsync_ops.py)
 *     GET     /api/v1/cloudsync/fleet    (same file)
 *     POST    /api/v1/cloudsync/enrollment-grants (same file)
 *
 * Unlike `api-cloudsync.ts`, these routes are in the generated OpenAPI
 * `paths`, so this module uses the shared `api` client with its real types:
 * a backend contract change breaks svelte-check here instead of drifting.
 * CLI twins: `python -m apps.sync_hub config show|set`, `sync`, `fleet --json`.
 */

import { api, unwrap } from './api/client';
import type { components } from './api-types';

type Schemas = components['schemas'];

export type CloudSyncConfigOut = Schemas['CloudSyncConfigOut'];
export type CloudSyncConfigBody = Schemas['CloudSyncConfig'];
export type SyncRunBody = Schemas['SyncRunIn'];
export type SyncRunOut = Schemas['SyncRunOut'];
export type FleetOut = Schemas['FleetOut'];
export type GrantIn = Schemas['GrantIn'];
export type GrantOut = Schemas['GrantOut'];
/** GrantIn with the server-defaulted ttl left optional: omitting it takes the
 * backend's own default (GrantIn.ttl_seconds) rather than a copy of it here. */
export type GrantRequest = Omit<GrantIn, 'ttl_seconds'> & { ttl_seconds?: number };

export async function getCloudSyncConfig(): Promise<CloudSyncConfigOut> {
	return unwrap(api.GET('/api/v1/cloudsync/config', {}));
}

export async function putCloudSyncConfig(body: CloudSyncConfigBody): Promise<CloudSyncConfigOut> {
	return unwrap(api.PUT('/api/v1/cloudsync/config', { body }));
}

export async function runCloudSyncNow(body: SyncRunBody): Promise<SyncRunOut> {
	return unwrap(api.POST('/api/v1/cloudsync/sync', { body }));
}

export async function getCloudSyncFleet(): Promise<FleetOut> {
	return unwrap(api.GET('/api/v1/cloudsync/fleet', {}));
}

export async function resumeCloudsyncSchedulerOwed(): Promise<{ ok: boolean }> {
	return unwrap(api.POST('/api/v1/cloudsync/scheduler/resume-owed', {}));
}

/**
 * Mint one single-use enrollment grant on this hub (`grant` CLI twin). The
 * token exists only in this response: callers show it once and never log it,
 * toast it or persist it.
 */
export async function mintEnrollmentGrant(body: GrantRequest): Promise<GrantOut> {
	return unwrap(api.POST('/api/v1/cloudsync/enrollment-grants', { body: body as GrantIn }));
}
