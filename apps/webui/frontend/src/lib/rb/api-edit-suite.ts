/**
 * Typed fetch client for the edit-suite batch: find-and-replace, bulk-edit,
 * mytag-editor (issues #180, #178, #179). Kept in its own module rather
 * than api-rb.ts/api.ts - those are single-owner hotspot files for this
 * fan-out wave (see (private path)); this file is net-new
 * so it carries no lane-ownership risk.
 *
 * CONVERTED onto the generated OpenAPI client (`src/lib/api/client.ts`); the
 * exported function signatures are unchanged, so call sites did not move.
 * Fail-fast, same as before: every helper throws with the backend's explicit
 * detail payload. No silent fallbacks.
 */

import type { components } from '../api-types';
import { ApiError, api, unwrap } from '../api/rb-http';
import { RbApiError } from './api-rb-error';
import type { TagCatalogEntry, TagTypeRow } from './tag-display';

/** One conflicting row from a `BatchConflictError` (see `backend.py`). */
export interface EditSuiteConflictRow {
	stable_id: string;
	current_etag: string;
}

/** Edit-suite routes put the machine code in `detail.error` (not
 * `detail.code`). Map from the parsed body so that contract stays intact.
 *
 * STATE-07/08/09: a 409 body already names exactly which rows failed their
 * ETag precondition (`detail.conflicts`, set by every one of bulk-edit,
 * find-replace and mytag's shared `BatchConflictError` handler) - this used
 * to discard that array before it reached `RbApiError`, so every caller saw
 * only "conflict: Conflict" with no way to say which row blocked it. Kept
 * on `.body` (RbApiError's existing raw-payload field) rather than adding a
 * new constructor param, matching the pattern beatgrid-upgrade.ts already
 * uses for a 404 detail field. */
function _toEditSuiteError(error: unknown): never {
	if (error instanceof ApiError) {
		const detail = (error.body as { detail?: { error?: string; message?: string } } | null)?.detail;
		throw new RbApiError(
			error.status,
			detail?.error ?? `HTTP_${error.status}`,
			error.message,
			error.body
		);
	}
	throw error;
}

/** Read the row-level detail off a conflict thrown by this module, or null
 * when the error is not a batch conflict (a different failure, or a
 * non-RbApiError entirely). Centralized so all three edit-suite modals read
 * the same shape the same way. */
export function editSuiteConflictRows(error: unknown): EditSuiteConflictRow[] | null {
	if (!(error instanceof RbApiError) || error.code !== 'conflict') return null;
	const conflicts = (error.body as { detail?: { conflicts?: unknown } } | null)?.detail?.conflicts;
	return Array.isArray(conflicts) ? (conflicts as EditSuiteConflictRow[]) : null;
}

// ----------------------------------------------------------- find-replace

export type FindReplaceRow = components['schemas']['FindReplaceRowOut'];

export type FindReplacePreviewResult = components['schemas']['FindReplacePreviewOut'];

/** Request scope kept hand-written: generated FindReplacePreviewIn marks
 * field/replace/mode/case_sensitive required (OpenAPI defaults), while the
 * exported client still accepts the optional form call sites already use. */
export interface FindReplaceScope {
	field?: 'notes' | 'genre' | 'comments';
	stable_ids: string[];
	find: string;
	replace?: string;
	mode?: 'literal' | 'regex';
	case_sensitive?: boolean;
}

export async function previewFindReplace(
	scope: FindReplaceScope
): Promise<FindReplacePreviewResult> {
	try {
		return await unwrap(
			api.POST('/api/v1/find-replace/preview', {
				body: scope as components['schemas']['FindReplacePreviewIn']
			})
		);
	} catch (error) {
		_toEditSuiteError(error);
	}
}

export type FindReplaceApplyResult = components['schemas']['FindReplaceApplyOut'];

export async function applyFindReplace(
	scope: FindReplaceScope & { expected_etags: Record<string, string> }
): Promise<FindReplaceApplyResult> {
	try {
		return await unwrap(
			api.POST('/api/v1/find-replace/apply', {
				body: scope as components['schemas']['FindReplaceApplyIn']
			})
		);
	} catch (error) {
		_toEditSuiteError(error);
	}
}

// --------------------------------------------------------------- bulk-edit

/** Request patch kept hand-written: generated BulkEditIn widens optional
 * fields with `| null`, which is not an exact match for this exported shape. */
export interface BulkEditPatch {
	stable_ids: string[];
	expected_etags: Record<string, string>;
	rating?: number;
	notes?: string;
	genre?: string;
	comments?: string;
	tags_add?: string[];
	tags_remove?: string[];
}

export type BulkEditResult = components['schemas']['BulkEditOut'];

export async function bulkEditTracks(patch: BulkEditPatch): Promise<BulkEditResult> {
	try {
		return await unwrap(
			api.PATCH('/api/v1/bulk-edit', {
				body: patch as components['schemas']['BulkEditIn']
			})
		);
	} catch (error) {
		_toEditSuiteError(error);
	}
}

// ---------------------------------------------------------------- mytags

export type MyTagSummary = components['schemas']['MyTagSummary'];

export type MyTagCatalog = components['schemas']['MyTagListOut'];

export async function listMyTags(): Promise<MyTagCatalog> {
	try {
		return await unwrap(api.GET('/api/v1/mytags', {}));
	} catch (error) {
		_toEditSuiteError(error);
	}
}

// ---------------------------------------------------------------- undoable tags
// Every write names this page load's undo stack, so Cmd+Z reverts only what this
// page did and a relaunch starts empty (PRD E31).

export type TagSuggestion = components['schemas']['TagSuggestionOut'];
type TagWrite = components['schemas']['WriteOut'];

const UNDO_SESSION = crypto.randomUUID();
const undoHeader = { 'X-Undo-Session': UNDO_SESSION };
/** When each tag write still on the undo stack happened, oldest first. */
const tagWriteTimes: number[] = [];

export function newestTagWriteAt(): number | null {
	return tagWriteTimes.at(-1) ?? null;
}

async function _write(request: () => Promise<TagWrite>): Promise<TagWrite> {
	const write = await _send(request);
	if (write.undo_id !== null) tagWriteTimes.push(Date.now());
	return write;
}

function _toTagError(error: unknown): never {
	if (error instanceof ApiError) {
		const detail = (error.body as { detail?: { error?: string; message?: string } } | null)?.detail;
		throw new RbApiError(
			error.status,
			detail?.error ?? `HTTP_${error.status}`,
			detail?.message ?? error.message,
			error.body
		);
	}
	throw error;
}

async function _send<T>(request: () => Promise<T>): Promise<T> {
	try {
		return await request();
	} catch (exc) {
		_toTagError(exc);
	}
}

export async function fetchTagCatalog(): Promise<TagCatalogEntry[]> {
	return (await _send(() => unwrap(api.GET('/api/v1/mytags/catalog', {})))).tags;
}

export async function fetchTagAssignments(): Promise<Record<string, string[]>> {
	return (await _send(() => unwrap(api.GET('/api/v1/mytags/assignments', {})))).tracks;
}

export async function fetchTagTypes(): Promise<TagTypeRow[]> {
	return (await _send(() => unwrap(api.GET('/api/v1/tag-types', {})))).rows;
}

export async function suggestTags(
	q: string,
	type: string | null,
	trackIds: readonly string[]
): Promise<TagSuggestion[]> {
	const query = { q, track_id: [...trackIds], ...(type === null ? {} : { type }) };
	return (await _send(() => unwrap(api.GET('/api/v1/mytags/suggest', { params: { query } }))))
		.suggestions;
}

export async function assignTags(body: {
	stable_ids: string[];
	add?: string[];
	remove?: string[];
	type?: string | null;
}): Promise<TagWrite> {
	return _write(() =>
		unwrap(
			api.POST('/api/v1/mytags/assignments', {
				params: { header: undoHeader },
				body: { add: [], remove: [], ...body }
			})
		)
	);
}

export async function renameTag(name: string, newName: string, merge: boolean): Promise<TagWrite> {
	return _write(() =>
		unwrap(
			api.PATCH('/api/v1/mytags/{name}', {
				params: { path: { name }, header: undoHeader },
				body: { name: newName, merge }
			})
		)
	);
}

export async function deleteTag(name: string): Promise<TagWrite> {
	return _write(() =>
		unwrap(api.DELETE('/api/v1/mytags/{name}', { params: { path: { name }, header: undoHeader } }))
	);
}

export async function patchTagType(
	id: string,
	changes: { show_as_column?: boolean; show_in_tags?: boolean }
): Promise<TagWrite> {
	return _write(() =>
		unwrap(
			api.PATCH('/api/v1/tag-types/{type_id}', {
				params: { path: { type_id: id }, header: undoHeader },
				body: changes
			})
		)
	);
}

export async function undoLastLibraryChange(): Promise<components['schemas']['UndoOut']> {
	const undone = await _send(() =>
		unwrap(api.POST('/api/v1/undo', { params: { header: undoHeader } }))
	);
	tagWriteTimes.pop();
	return undone;
}
