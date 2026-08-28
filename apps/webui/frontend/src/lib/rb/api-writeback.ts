import type { components } from '../api-types';
import { ApiError, api, unwrap } from '../api/client';
import { RbApiError } from './api-rb';

export type WritebackVendor = 'rekordbox' | 'djay';
export type WritebackTargetMode = 'live';

/** Kept hand-written: generated VendorCapabilityOut uses vendor:string and optional nullables. */
export interface VendorCapability { vendor: WritebackVendor; available: boolean; target_mode: WritebackTargetMode; target_path: string | null; reason: string | null; }
/** Kept hand-written: depends on VendorCapability above. */
export interface WritebackCapabilities { playlist_id: string; vendors: VendorCapability[]; }
export type WritebackTarget = components['schemas']['WritebackTargetOut'];
export type WritebackPlan = components['schemas']['WritebackPlanOut'];
/** Kept hand-written: generated WritebackApplyOut makes backup_id/target_revision/error optional. */
export interface WritebackApplyResult {
	playlist_id: string; vendor: WritebackVendor; target_id: string; target_name: string; applied: boolean;
	dry_run: boolean; added: string[]; removed: string[]; backup_id: string | null; target_revision: string | null; error: string | null;
}
export type WritebackRollbackResult = components['schemas']['WritebackRollbackOut'];

function _toWritebackError(error: unknown): never {
	if (error instanceof ApiError) throw new RbApiError(error.status, error.code, error.message);
	throw error;
}

export async function getWritebackCapabilities(playlistId: string): Promise<WritebackCapabilities> {
	try {
		return await unwrap(api.GET('/api/v1/playlists/{playlist_id}/writeback/capabilities', { params: { path: { playlist_id: playlistId } } })) as WritebackCapabilities;
	} catch (error) { _toWritebackError(error); }
}
export async function getWritebackTargets(playlistId: string, vendor: WritebackVendor, mode: WritebackTargetMode, path: string): Promise<{ targets: WritebackTarget[] }> {
	try {
		return await unwrap(api.GET('/api/v1/playlists/{playlist_id}/writeback/targets', { params: { path: { playlist_id: playlistId }, query: { vendor, target_mode: mode, target_path: path } } }));
	} catch (error) { _toWritebackError(error); }
}
export async function getWritebackPlan(playlistId: string, vendor: WritebackVendor, mode: WritebackTargetMode, path: string, id: string): Promise<WritebackPlan> {
	try {
		return await unwrap(api.GET('/api/v1/playlists/{playlist_id}/writeback/plan', { params: { path: { playlist_id: playlistId }, query: { vendor, target_mode: mode, target_path: path, target_id: id } } }));
	} catch (error) { _toWritebackError(error); }
}
export async function applyWriteback(playlistId: string, plan: WritebackPlan): Promise<WritebackApplyResult> {
	try {
		return await unwrap(api.POST('/api/v1/playlists/{playlist_id}/writeback/apply', { params: { path: { playlist_id: playlistId } }, body: { vendor: plan.vendor, target_mode: plan.target_mode, target_path: plan.target_path, target_id: plan.target_id, plan_token: plan.plan_token, dry_run: false, confirmed: true } })) as WritebackApplyResult;
	} catch (error) { _toWritebackError(error); }
}
export async function rollbackWriteback(playlistId: string, result: WritebackApplyResult, plan: WritebackPlan): Promise<WritebackRollbackResult> {
	if (!result.backup_id || !result.target_revision) throw new Error('apply result has no reversible backup');
	try {
		return await unwrap(api.POST('/api/v1/playlists/{playlist_id}/writeback/rollback', { params: { path: { playlist_id: playlistId } }, body: { vendor: plan.vendor, target_mode: plan.target_mode, target_path: plan.target_path, target_id: plan.target_id, backup_id: result.backup_id, expected_target_revision: result.target_revision, confirmed: true } }));
	} catch (error) { _toWritebackError(error); }
}
