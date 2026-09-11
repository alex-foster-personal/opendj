/**
 * Typed client for /api/v1/ingest (config, coverage, refresh job, upload) and
 * the analyze-on-import queue at GET /api/v1/analysis-queue.
 *
 * There is no client for POST /analysis-queue/run on purpose. The drain is
 * started by the daemon's own reconcile loop, and the TopBar's existing
 * "Refresh analysis" button already covers the manual case; the run endpoint
 * stays mounted for agents driving the daemon over HTTP, which need no TS.
 *
 * Kept separate from api-rb.ts (fan-out hotspot). Same conventions: relative
 * API_BASE, fail-fast RbApiError on !ok, no invented data.
 */

import { API_BASE } from '$lib/api';
import { RbApiError } from './api-rb-error';

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
	/** Per-step count of entries that PARSE-FAIL their real contract (malformed JSON, invalid fields, identity mismatch) - a subset of `missing`, distinct from an ordinary not-yet-run or stale-needs-rerun verdict. */
	corrupt: Record<string, number>;
	generated_at: number;
};

/** Fired with the fresh config after every successful putIngestConfig, so a
 * module-scope cache of ingest config elsewhere (AnalysisDotsPopover.svelte's
 * shared 15s-TTL cache, see its `_sharedIngestConfig`) can update itself the
 * instant the config actually changes, instead of serving a stale verdict
 * for up to its own TTL. Deliberately a plain listener set, not a store: the
 * cache is not reactive UI state, and this keeps the two modules decoupled
 * (api-ingest.ts does not need to know its callers exist). */
type IngestConfigWriteListener = (cfg: IngestConfig) => void;
const _writeListeners = new Set<IngestConfigWriteListener>();

export function onIngestConfigWrite(listener: IngestConfigWriteListener): void {
	_writeListeners.add(listener);
}

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

/** One track waiting to be analyzed because it has no vendor mapping. */
export type AnalysisQueueItem = {
	stable_id: string;
	file_path: string;
	title: string | null;
};

/** State of the daemon's analyze-on-import reconcile loop. */
export type AutoAnalyze = {
	enabled: boolean;
	interval_s: number;
	attempts: number;
	last_started_at: number | null;
	last_signature: string | null;
	last_outcome: string | null;
};

/**
 * The analyze-on-import queue. `analyzed + unreachable + pending` always
 * equals `unmapped`; `items` is a page of `pending` and never moves a count.
 */
export type AnalysisQueue = {
	unmapped: number;
	pending: number;
	analyzed: number;
	unreachable: number;
	signature: string;
	items: AnalysisQueueItem[];
	job: RefreshStatus;
	auto: AutoAnalyze;
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
	const cfg = (await r.json()) as IngestConfig;
	for (const listener of _writeListeners) listener(cfg);
	return cfg;
}

export async function getIngestCoverage(): Promise<IngestCoverage> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/coverage`);
	if (!r.ok) await _err(r);
	return (await r.json()) as IngestCoverage;
}

export async function startIngestRefresh(batchDir?: string): Promise<RefreshStatus> {
	// batchDir scopes the refresh to freshly staged files (server validates it
	// lives under the ingest inbox); omitted = whole-library coverage sweep.
	const r = await fetch(`${API_BASE}/api/v1/ingest/refresh`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(batchDir ? { batch_dir: batchDir } : {})
	});
	if (!r.ok) await _err(r);
	return (await r.json()) as RefreshStatus;
}

export async function getIngestRefreshStatus(): Promise<RefreshStatus> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/refresh/status`);
	if (!r.ok) await _err(r);
	return (await r.json()) as RefreshStatus;
}

export async function getAnalysisQueue(limit?: number): Promise<AnalysisQueue> {
	// limit pages `items` only; the counts always describe the whole queue.
	const query = limit === undefined ? '' : `?limit=${limit}`;
	const r = await fetch(`${API_BASE}/api/v1/analysis-queue${query}`);
	if (!r.ok) await _err(r);
	return (await r.json()) as AnalysisQueue;
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
