import { RB_API_BASE, RbApiError } from './api-rb';

export type WritebackVendor = 'rekordbox' | 'djay';
export type WritebackTargetMode = 'live';

export interface VendorCapability { vendor: WritebackVendor; available: boolean; target_mode: WritebackTargetMode; target_path: string | null; reason: string | null; }
export interface WritebackCapabilities { playlist_id: string; vendors: VendorCapability[]; }
export interface WritebackTarget { playlist_id: string; name: string; }
export interface WritebackPlan {
	playlist_id: string; vendor: WritebackVendor; target_mode: WritebackTargetMode; target_path: string;
	target_id: string; target_name: string; source_revision: string; target_revision: string; mapping_revision: string; ordered_match: boolean; plan_token: string;
	added: string[]; removed: string[]; unresolved: string[]; is_noop: boolean;
}
export interface WritebackApplyResult {
	playlist_id: string; vendor: WritebackVendor; target_id: string; target_name: string; applied: boolean;
	dry_run: boolean; added: string[]; removed: string[]; backup_id: string | null; target_revision: string | null; error: string | null;
}
export interface WritebackRollbackResult { playlist_id: string; vendor: WritebackVendor; target_id: string; backup_id: string; rolled_back: boolean; target_revision: string; }

async function _throwWritebackError(r: Response): Promise<never> {
	const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
	const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
	throw new RbApiError(r.status, detail?.code ?? `HTTP_${r.status}`, detail?.message ?? r.statusText);
}
async function _get<T>(path: string): Promise<T> { const r = await fetch(`${RB_API_BASE}${path}`, { headers: { Accept: 'application/json' } }); if (!r.ok) await _throwWritebackError(r); return (await r.json()) as T; }
async function _post<T>(path: string, body: object): Promise<T> { const r = await fetch(`${RB_API_BASE}${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) }); if (!r.ok) await _throwWritebackError(r); return (await r.json()) as T; }
const _targetQuery = (mode: WritebackTargetMode, path: string, id?: string): string => `target_mode=${mode}&target_path=${encodeURIComponent(path)}${id ? `&target_id=${encodeURIComponent(id)}` : ''}`;

export function getWritebackCapabilities(playlistId: string): Promise<WritebackCapabilities> { return _get(`/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/capabilities`); }
export function getWritebackTargets(playlistId: string, vendor: WritebackVendor, mode: WritebackTargetMode, path: string): Promise<{ targets: WritebackTarget[] }> { return _get(`/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/targets?vendor=${vendor}&${_targetQuery(mode, path)}`); }
export function getWritebackPlan(playlistId: string, vendor: WritebackVendor, mode: WritebackTargetMode, path: string, id: string): Promise<WritebackPlan> { return _get(`/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/plan?vendor=${vendor}&${_targetQuery(mode, path, id)}`); }
export function applyWriteback(playlistId: string, plan: WritebackPlan): Promise<WritebackApplyResult> { return _post(`/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/apply`, { vendor: plan.vendor, target_mode: plan.target_mode, target_path: plan.target_path, target_id: plan.target_id, plan_token: plan.plan_token, dry_run: false, confirmed: true }); }
export function rollbackWriteback(playlistId: string, result: WritebackApplyResult, plan: WritebackPlan): Promise<WritebackRollbackResult> {
	if (!result.backup_id || !result.target_revision) throw new Error('apply result has no reversible backup');
	return _post(`/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/rollback`, { vendor: plan.vendor, target_mode: plan.target_mode, target_path: plan.target_path, target_id: plan.target_id, backup_id: result.backup_id, expected_target_revision: result.target_revision, confirmed: true });
}
