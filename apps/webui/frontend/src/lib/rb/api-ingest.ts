/**
 * Typed client for /api/v1/ingest (config, coverage, refresh job, upload).
 *
 * Kept separate from api-rb.ts (fan-out hotspot). Same conventions: relative
 * API_BASE, fail-fast RbApiError on !ok, no invented data.
 */

import { API_BASE } from '$lib/api';
import { RbApiError } from './api-rb';

export type IngestStep = {
	id: 'analysis' | 'stems' | 'vocals';
	label: string;
	enabled: boolean;
	default_enabled: boolean;
	requires_rb_row: boolean;
	hint: string;
};

export type IngestConfig = { steps: IngestStep[]; path: string };

export type IngestCoverage = {
	total_tracks: number;
	on_disk: number;
	unreachable: number;
	missing: Record<string, number>;
	generated_at: number;
};

export type RefreshStatus = {
	running: boolean;
	phase: 'idle' | 'queued' | 'running' | 'done' | 'error';
	steps: string[];
	current_step: string | null;
	step_done: number;
	step_total: number;
	steps_completed: string[];
	started_at: number | null;
	finished_at: number | null;
	error: string | null;
	log_tail: string[];
	recently_done_ids: string[];
};

export type UploadFileResult = {
	filename: string;
	staged_path: string | null;
	skipped_duplicate: boolean;
	duplicate_of: {
		stable_id: string;
		title: string | null;
		artist: string | null;
		method: 'chromaprint' | 'duration';
		score: number | null;
	} | null;
	duration_s: number | null;
	fingerprint_method: 'chromaprint' | 'duration';
};

export type UploadOut = { batch: string; dest_dir: string; results: UploadFileResult[] };

async function _err(r: Response): Promise<never> {
	const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
	const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
	const msg = typeof body.detail === 'string' ? body.detail : detail?.message;
	throw new RbApiError(r.status, detail?.code ?? `HTTP_${r.status}`, msg ?? r.statusText);
}

export async function getIngestConfig(): Promise<IngestConfig> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/config`);
	if (!r.ok) await _err(r);
	return (await r.json()) as IngestConfig;
}

export async function putIngestConfig(enabled: Record<string, boolean>): Promise<IngestConfig> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/config`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ enabled })
	});
	if (!r.ok) await _err(r);
	return (await r.json()) as IngestConfig;
}

export async function getIngestCoverage(): Promise<IngestCoverage> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/coverage`);
	if (!r.ok) await _err(r);
	return (await r.json()) as IngestCoverage;
}

export async function startIngestRefresh(): Promise<RefreshStatus> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/refresh`, { method: 'POST' });
	if (!r.ok) await _err(r);
	return (await r.json()) as RefreshStatus;
}

export async function getIngestRefreshStatus(): Promise<RefreshStatus> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/refresh/status`);
	if (!r.ok) await _err(r);
	return (await r.json()) as RefreshStatus;
}

export async function uploadIngestFiles(
	files: File[],
	batch: string,
	force = false
): Promise<UploadOut> {
	const form = new FormData();
	for (const f of files) form.append('files', f, f.name);
	form.append('batch', batch);
	form.append('force', String(force));
	const r = await fetch(`${API_BASE}/api/v1/ingest/upload`, { method: 'POST', body: form });
	if (!r.ok) await _err(r);
	return (await r.json()) as UploadOut;
}
