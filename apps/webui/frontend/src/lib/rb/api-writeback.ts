/**
 * Typed fetch client for the playlist writeback router
 * (LANE playlists-router, node write-back-rekordbox-djay --
 * apps/webui/server/routes/playlist_writeback.py).
 *
 * Lives in its OWN module, same convention as api-smartlists.ts: api-rb.ts
 * and types.ts are wave hotspots owned by the integrator.
 *
 * Fail-fast: same RbApiError contract as api-rb.ts -- every non-ok
 * response throws the backend's explicit {"detail": {code, message}}.
 * Known code: WRITEBACK_VENDOR_UNAVAILABLE (503, vendor DB unreachable).
 */

import { RB_API_BASE, RbApiError } from './api-rb';

// ----------------------------------------------------------- types

export type WritebackVendor = 'rekordbox' | 'djay';

export interface VendorCapability {
	vendor: string;
	available: boolean;
	reason: string | null;
}

export interface WritebackCapabilities {
	playlist_id: string;
	vendors: VendorCapability[];
}

export interface WritebackPlan {
	playlist_id: string;
	vendor: string;
	playlist_name: string;
	target_exists: boolean;
	added: string[];
	removed: string[];
	unresolved: string[];
	is_noop: boolean;
}

export interface WritebackApplyResult {
	playlist_id: string;
	vendor: string;
	playlist_name: string;
	applied: boolean;
	dry_run: boolean;
	added: string[];
	removed: string[];
	error: string | null;
}

// ----------------------------------------------------------- _helpers

async function _throwWritebackError(r: Response): Promise<never> {
	const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
	const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
	throw new RbApiError(r.status, detail?.code ?? `HTTP_${r.status}`, detail?.message ?? r.statusText);
}

async function _get<T>(path: string): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, { headers: { Accept: 'application/json' } });
	if (!r.ok) await _throwWritebackError(r);
	return (await r.json()) as T;
}

// ----------------------------------------------------------- fetchers

export async function getWritebackCapabilities(playlistId: string): Promise<WritebackCapabilities> {
	return _get<WritebackCapabilities>(
		`/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/capabilities`
	);
}

export async function getWritebackPlan(
	playlistId: string,
	vendor: WritebackVendor
): Promise<WritebackPlan> {
	return _get<WritebackPlan>(
		`/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/plan?vendor=${vendor}`
	);
}

export async function applyWriteback(
	playlistId: string,
	vendor: WritebackVendor,
	opts: { dry_run?: boolean; force_adopt?: boolean } = {}
): Promise<WritebackApplyResult> {
	const r = await fetch(`${RB_API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}/writeback/apply`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({
			vendor,
			dry_run: opts.dry_run ?? true,
			force_adopt: opts.force_adopt ?? false
		})
	});
	if (!r.ok) await _throwWritebackError(r);
	return (await r.json()) as WritebackApplyResult;
}
