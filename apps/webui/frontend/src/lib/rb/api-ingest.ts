/**
 * Typed client for /api/v1/ingest (config, coverage, refresh job, upload) and
 * the analyze-on-import queue at GET /api/v1/analysis-queue.
 *
 * POST /analysis-queue/run has a client because first-run setup needs it. The
 * daemon's reconcile loop does start the drain on its own, but on a timer: a
 * folder import is the one path with no rekordbox analysis to fall back on, so
 * the wizard's last screen asks for the drain NOW rather than showing a user a
 * library with no BPM, key or beatgrid for up to a full interval. Everywhere
 * else still leaves it to the loop or to the TopBar's "Refresh analysis".
 *
 * Kept separate from api-rb.ts (fan-out hotspot). Same conventions: relative
 * API_BASE, fail-fast RbApiError on !ok, no invented data.
 */

import { API_BASE, timeoutSignal } from '$lib/api';
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

/** GET /ingest/coverage. `on_disk` is the `present` denominator; per step,
 * `done + terminal + failed + pending === on_disk`. */
export type IngestCoverage = {
	total_tracks: number;
	on_disk: number;
	unreachable: number;
	missing: Record<string, number>;
	/** Per-step count of entries that PARSE-FAIL their real contract (malformed JSON, invalid fields, identity mismatch) - a subset of `missing`, distinct from an ordinary not-yet-run or stale-needs-rerun verdict. */
	corrupt: Record<string, number>;
	/** Where every live row's audio stands on this machine. */
	availability: Record<string, number>;
	done: Record<string, number>;
	/** Nothing to make (no lyrics available, no stems source): finished, not done. */
	terminal: Record<string, number>;
	/** A drain job failed its last allowed attempt on this audio file. */
	failed: Record<string, number>;
	pending: Record<string, number>;
	waiting_on_stems: number;
	stems_source_refusal: string | null;
	/** Stem bundles on this disk; `local.stems + in_cloud.stems === done.stems`. */
	local: Record<string, number>;
	/** Stem bundles evicted to R2 that this machine can fetch back. Done, not pending. */
	in_cloud: Record<string, number>;
	/** Vocals pending whose bundle is in the cloud (the drain fetches one at a time). */
	awaiting_stem_download: number;
	stems_index: { state: 'ok' | 'off' | 'unknown'; reason: string | null };
	generated_at: number;
};

/** Warm coverage answers in under 2 s; a cold engine can take far longer.
 * Past this the dots go grey "unknown" and the next refetch asks again. */
export const INGEST_COVERAGE_TIMEOUT_MS = 15_000;

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

export type AnalysisOrder = {
	stable_id: string;
	kind: string;
	phase: 'queued' | 'running' | 'done' | 'error';
};

export type UploadVerdict = 'new' | 'possible_duplicate' | 'skipped_duplicate';

export type UploadFileResult = {
	filename: string;
	staged_path: string | null;
	skipped_duplicate: boolean;
	verdict: UploadVerdict;
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

export type PendingBatch = {
	name: string;
	dest_dir: string;
	file_count: number;
	awaiting_rb: boolean;
};

export type PendingOut = { batches: PendingBatch[] };

export type UploadOut = { batch: string; dest_dir: string; results: UploadFileResult[] };

export type MaterializedTrack = {
	relative_path: string;
	stable_id: string;
	inserted: boolean;
};

export type MaterializeOut = { batch: string; tracks: MaterializedTrack[] };

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

export async function getIngestCoverage(
	timeoutMs: number = INGEST_COVERAGE_TIMEOUT_MS
): Promise<IngestCoverage> {
	const { signal, clear } = timeoutSignal(timeoutMs);
	try {
		const r = await fetch(`${API_BASE}/api/v1/ingest/coverage`, { signal });
		if (!r.ok) await _err(r);
		return (await r.json()) as IngestCoverage;
	} catch (error: unknown) {
		if (signal.aborted) {
			throw new Error(`coverage request timed out after ${timeoutMs / 1000} s`, { cause: error });
		}
		throw error;
	} finally {
		clear();
	}
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

/**
 * Start the unmapped-scope drain now. 409 when a refresh job already holds the
 * one slot, which is not an error the caller has to handle as a failure: it
 * means the work this call wanted is already happening.
 */
export async function startAnalysisQueueDrain(): Promise<RefreshStatus | null> {
	const r = await fetch(`${API_BASE}/api/v1/analysis-queue/run`, { method: 'POST' });
	if (r.status === 409) return null;
	if (!r.ok) await _err(r);
	return (await r.json()) as RefreshStatus;
}

export async function getTrackAnalysisOrders(stableId: string): Promise<AnalysisOrder[]> {
	const r = await fetch(`${API_BASE}/api/v1/analysis-queue/orders/${encodeURIComponent(stableId)}`);
	if (!r.ok) await _err(r);
	return ((await r.json()) as { items: AnalysisOrder[] }).items;
}

export async function orderTrackAnalysis(stableId: string, kind: string): Promise<AnalysisOrder> {
	const r = await fetch(
		`${API_BASE}/api/v1/analysis-queue/orders/${encodeURIComponent(stableId)}/${encodeURIComponent(kind)}`,
		{ method: 'POST' }
	);
	if (!r.ok) await _err(r);
	return (await r.json()) as AnalysisOrder;
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

export async function decideIngestUpload(opts: {
	batch: string;
	filename: string;
	action: 'accept' | 'reject';
}): Promise<UploadFileResult> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/upload/decide`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(opts)
	});
	if (!r.ok) await _err(r);
	return (await r.json()) as UploadFileResult;
}

export async function getIngestPending(): Promise<PendingOut> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/pending`);
	if (!r.ok) await _err(r);
	return (await r.json()) as PendingOut;
}

export async function materializeIngestBatch(batch: string): Promise<MaterializeOut> {
	const r = await fetch(
		`${API_BASE}/api/v1/ingest/batch/${encodeURIComponent(batch)}/materialize`,
		{ method: 'POST' }
	);
	if (!r.ok) await _err(r);
	return (await r.json()) as MaterializeOut;
}

export async function confirmIngestPending(batch: string): Promise<void> {
	const r = await fetch(`${API_BASE}/api/v1/ingest/pending/${encodeURIComponent(batch)}/confirm`, {
		method: 'POST'
	});
	if (!r.ok) await _err(r);
}
