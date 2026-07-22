/**
 * Typed fetch client for the edit-suite batch: find-and-replace, bulk-edit,
 * mytag-editor (issues #180, #178, #179). Kept in its own module rather
 * than api-rb.ts/api.ts - those are single-owner hotspot files for this
 * fan-out wave (see .planning/FANOUT-CONVENTIONS.md); this file is net-new
 * so it carries no lane-ownership risk.
 *
 * Fail-fast, same as api-rb.ts: every helper checks r.ok and throws with
 * the backend's explicit detail payload. No silent fallbacks.
 */

import { RB_API_BASE, RbApiError } from './api-rb';

async function _throwEditSuiteError(r: Response): Promise<never> {
	const body = (await r.json().catch(() => ({}))) as {
		detail?: { error?: string; message?: string; conflicts?: unknown } | string;
	};
	const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
	throw new RbApiError(
		r.status,
		(detail?.error as string | undefined) ?? `HTTP_${r.status}`,
		detail?.message ?? r.statusText
	);
}

async function _postJson<T>(path: string, body: unknown): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(body)
	});
	if (!r.ok) await _throwEditSuiteError(r);
	return (await r.json()) as T;
}

async function _patchJson<T>(path: string, body: unknown): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(body)
	});
	if (!r.ok) await _throwEditSuiteError(r);
	return (await r.json()) as T;
}

// ----------------------------------------------------------- find-replace

export interface FindReplaceRow {
	stable_id: string;
	current_value: string | null;
	new_value: string | null;
	would_change: boolean;
	etag: string;
}

export interface FindReplacePreviewResult {
	results: FindReplaceRow[];
	match_count: number;
}

export interface FindReplaceScope {
	field?: 'notes';
	stable_ids: string[];
	find: string;
	replace?: string;
	mode?: 'literal' | 'regex';
	case_sensitive?: boolean;
}

export async function previewFindReplace(scope: FindReplaceScope): Promise<FindReplacePreviewResult> {
	return _postJson('/api/v1/find-replace/preview', scope);
}

export interface FindReplaceApplyResult {
	applied_count: number;
	skipped_noop: string[];
	results: { stable_id: string; new_value: string | null; etag: string }[];
}

export async function applyFindReplace(
	scope: FindReplaceScope & { expected_etags: Record<string, string> }
): Promise<FindReplaceApplyResult> {
	return _postJson('/api/v1/find-replace/apply', scope);
}

// --------------------------------------------------------------- bulk-edit

export interface BulkEditPatch {
	stable_ids: string[];
	expected_etags: Record<string, string>;
	rating?: number;
	notes?: string;
	tags_add?: string[];
	tags_remove?: string[];
}

export interface BulkEditResult {
	applied_count: number;
	results: { stable_id: string; etag: string }[];
}

export async function bulkEditTracks(patch: BulkEditPatch): Promise<BulkEditResult> {
	return _patchJson('/api/v1/bulk-edit', patch);
}

// ---------------------------------------------------------------- mytags

export interface MyTagSummary {
	name: string;
	track_count: number;
}

export interface MyTagCatalog {
	tags: MyTagSummary[];
	catalog_revision: string;
}

export async function listMyTags(): Promise<MyTagCatalog> {
	const r = await fetch(`${RB_API_BASE}/api/v1/mytags`, { headers: { Accept: 'application/json' } });
	if (!r.ok) await _throwEditSuiteError(r);
	return (await r.json()) as MyTagCatalog;
}

export interface MyTagAssignResult {
	applied_count: number;
	results: { stable_id: string; tags: string[]; etag: string }[];
}

export async function assignMyTags(body: {
	stable_ids: string[];
	expected_etags: Record<string, string>;
	add?: string[];
	remove?: string[];
}): Promise<MyTagAssignResult> {
	return _postJson('/api/v1/mytags/assign', body);
}

export interface MyTagSweepPrecondition {
	expected_catalog_revision: string;
	expected_track_count: number;
}

export async function renameMyTag(
	body: MyTagSweepPrecondition & {
		old_name: string;
		new_name: string;
		confirm_merge: boolean;
	}
): Promise<{ tracks_updated: number }> {
	return _postJson('/api/v1/mytags/rename', body);
}

export async function deleteMyTag(
	body: MyTagSweepPrecondition & { name: string }
): Promise<{ tracks_updated: number }> {
	return _postJson('/api/v1/mytags/delete', body);
}
