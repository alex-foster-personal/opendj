/**
 * Edge sampler: typed client for `/api/v1/sampler/*` (server: apps/webui/server/routes/sampler.py,
 * bodies: apps/webui/server/sampler_models.py). Every non-2xx response throws with the status and
 * the server's body; nothing is swallowed. Each call takes an optional `fetchFn` (default: the
 * global fetch) so a caller or a test can supply its own transport.
 */

import { API_BASE } from '$lib/api/base';
import type {
	CaptureRequest,
	SamplerSlot,
	SetMeta,
	SlotSettings,
	SlotSort,
	SlotsPage
} from './sampler-types';

const SAMPLER = `${API_BASE}/api/v1/sampler`;
/** Each clip carries this much extra audio before and after the region (header X-Guard-Ms). */
export const CLIP_GUARD_MS = 20;

export type FetchFn = typeof fetch;
export type StemPartName = 'vocals' | 'drums' | 'bass' | 'inst';

export interface SlotsQuery {
	sort: SlotSort;
	q: string;
	offset: number;
	limit: number;
	master_bpm?: number;
	master_key?: string;
}

/** PATCH /slots/{id} body (sampler_models.SlotPatch): an omitted field keeps its value. */
export interface SlotPatch {
	/** null or '' clears the rename */
	name?: string | null;
	description?: string;
	settings?: SlotSettings;
}

/** PUT /sets/{id} body (sampler_models.SetPut). */
export interface SetPut {
	name: string;
	description: string;
	slot_ids: Array<string | null> | null;
}

/** A non-2xx sampler answer: the HTTP status and the server's raw body. */
export class SamplerApiError extends Error {
	constructor(
		readonly status: number,
		readonly body: string,
		what: string
	) {
		super(`sampler ${what} failed: HTTP ${status} ${body.slice(0, 200)}`);
	}
}

async function _send<T>(
	fetchFn: FetchFn,
	what: string,
	path: string,
	init: RequestInit = {}
): Promise<T> {
	const headers: Record<string, string> = { Accept: 'application/json' };
	if (init.body !== undefined) headers['Content-Type'] = 'application/json';
	const r = await fetchFn(`${SAMPLER}${path}`, { ...init, headers });
	if (!r.ok) throw new SamplerApiError(r.status, await r.text(), what);
	if (r.status === 204) return undefined as T;
	return (await r.json()) as T;
}

const slotPath = (slotId: string): string => `/slots/${encodeURIComponent(slotId)}`;

function _array<T>(body: Record<string, unknown> | null, key: string, what: string): T[] {
	const value = body?.[key];
	if (!Array.isArray(value)) throw new Error(`sampler: ${what} has no ${key} array`);
	return value as T[];
}

/** GET /slots: one ranked page of the slot catalogue. */
export function fetchSamplerSlots(query: SlotsQuery, fetchFn: FetchFn = fetch): Promise<SlotsPage> {
	const p = new URLSearchParams({
		sort: query.sort,
		q: query.q,
		offset: String(query.offset),
		limit: String(query.limit)
	});
	if (query.master_bpm !== undefined) p.set('master_bpm', String(query.master_bpm));
	if (query.master_key !== undefined) p.set('master_key', query.master_key);
	return _send(fetchFn, 'list slots', `/slots?${p.toString()}`);
}

/** GET /slots/{id}: one listed slot (404 SLOT_NOT_FOUND throws). */
export function fetchSamplerSlot(slotId: string, fetchFn: FetchFn = fetch): Promise<SamplerSlot> {
	return _send(fetchFn, 'get slot', slotPath(slotId));
}

/** PATCH /slots/{id}: save a slot's name, description and settings, answering the updated slot. */
export function patchSamplerSlot(
	slotId: string,
	patch: SlotPatch,
	fetchFn: FetchFn = fetch
): Promise<SamplerSlot> {
	return _send(fetchFn, 'update slot', slotPath(slotId), {
		method: 'PATCH',
		body: JSON.stringify(patch)
	});
}

/** POST /slots: capture a deck loop into a set, answering the new slot (201). */
export function postSamplerCapture(
	req: CaptureRequest,
	fetchFn: FetchFn = fetch
): Promise<SamplerSlot> {
	return _send(fetchFn, 'capture', '/slots', { method: 'POST', body: JSON.stringify(req) });
}

/** DELETE /slots/{id}: remove a capture (204); a cue slot is 409 NOT_A_CAPTURE. */
export async function deleteSamplerSlot(slotId: string, fetchFn: FetchFn = fetch): Promise<void> {
	await _send<unknown>(fetchFn, 'delete slot', slotPath(slotId), { method: 'DELETE' });
}

/** GET /sets answers `{ sets: [...] }` (sampler.py list_sets), not a bare array. */
export async function fetchSamplerSets(fetchFn: FetchFn = fetch): Promise<SetMeta[]> {
	return _array<SetMeta>(await _send(fetchFn, 'list sets', '/sets'), 'sets', 'GET /sets');
}

/** PUT /sets/{id}: save one set's name, description and layout, answering it as stored. */
export function putSamplerSet(
	id: string,
	body: SetPut,
	fetchFn: FetchFn = fetch
): Promise<SetMeta> {
	return _send(fetchFn, 'save set', `/sets/${encodeURIComponent(id)}`, {
		method: 'PUT',
		body: JSON.stringify(body)
	});
}

/** GET /pins answers `{ pins: slot_id[] }`, oldest pin first (sampler.py list_pins). */
export async function fetchSamplerPins(fetchFn: FetchFn = fetch): Promise<string[]> {
	return _array<string>(await _send(fetchFn, 'list pins', '/pins'), 'pins', 'GET /pins');
}

/** PUT /pins/{id}: persist one pin in sampler.db and return the server's full pin list. */
export async function putSamplerPin(
	slotId: string,
	pinned: boolean,
	fetchFn: FetchFn = fetch
): Promise<string[]> {
	const body = await _send<Record<string, unknown>>(
		fetchFn,
		'save pin',
		`/pins/${encodeURIComponent(slotId)}`,
		{ method: 'PUT', body: JSON.stringify({ pinned }) }
	);
	return _array<string>(body, 'pins', 'PUT /pins');
}

/** FLAC clip of the region plus CLIP_GUARD_MS either side; `part` cuts it from the stem-set. */
export function clipUrl(slotId: string, part?: StemPartName): string {
	return `${SAMPLER}/clips/${encodeURIComponent(slotId)}${part === undefined ? '' : `?part=${part}`}`;
}

/** Fetch a clip's bytes and check the guard header matches the contract. */
export async function fetchClipBytes(
	slotId: string,
	part?: StemPartName,
	fetchFn: FetchFn = fetch
): Promise<ArrayBuffer> {
	const what = `clip ${slotId}${part === undefined ? '' : ` ${part}`}`;
	const r = await fetchFn(clipUrl(slotId, part));
	if (!r.ok) throw new SamplerApiError(r.status, await r.text(), what);
	const guard = r.headers.get('X-Guard-Ms');
	if (guard !== String(CLIP_GUARD_MS))
		throw new Error(`${what}: expected X-Guard-Ms ${CLIP_GUARD_MS}, got ${guard}`);
	return r.arrayBuffer();
}
