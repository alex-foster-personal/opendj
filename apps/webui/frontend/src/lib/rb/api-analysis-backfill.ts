/**
 * Typed client for the native-analysis v1 backfill queue,
 * /api/v1/analysis/backfill/* (NATIVE-10).
 *
 * One function per queue control, and each one is the UI half of a triple the
 * repo's agent-native parity rule requires: HTTP endpoint, CLI subcommand
 * (`python -m apps.analysis.queue_cli <verb>`), UI control. A parity test
 * (tests/webui/test_queue_routes.py) reads THIS file and the panel to check
 * the third leg exists, so a control added here without its endpoint or its
 * CLI command fails the suite rather than shipping as a browser-only flow.
 *
 * Deliberately NOT a client for the drain itself. A drain holds a process
 * pool for minutes to hours, so it is a CLI process
 * (`python -m apps.analysis.queue_cli run`), never a request the daemon
 * serves. The panel plans, watches and steers; it does not run the analysis.
 *
 * Kept out of api-rb.ts (fan-out hotspot). Same conventions: relative
 * API_BASE, fail-fast RbApiError on !ok, no invented data.
 */

import { API_BASE } from '$lib/api';
import { RbApiError } from './api-rb';

/** The measured peak-RSS model a batch was budgeted under. Carried on every
 * response because floor and slope are MEASURED numbers for one producer
 * version, not constants: the panel shows where they were measured. */
export type BackfillMemoryModel = {
	backend: string;
	producer_version: string;
	floor_mb: number;
	slope_mb_per_min: number;
	measured_on: string;
	source: string;
};

export type BackfillItemState =
	| 'pending'
	| 'running'
	| 'done'
	| 'skipped'
	| 'failed'
	| 'refused'
	| 'cancelled';

export type BackfillItem = {
	stable_id: string;
	lane: string;
	backend: string;
	state: BackfillItemState;
	/** Named cause for a refusal, a failure or a skip. Never null on those. */
	reason: string | null;
	attempts: number;
	duration_s: number | null;
	predicted_peak_mb: number | null;
};

export type BackfillBand = 'under_20_min' | '20_to_45_min' | 'over_45_min' | 'empty';

export type BackfillEnqueueResult = {
	batch_id: string;
	/** offered = admitted + refused, always. */
	offered: number;
	admitted: number;
	refused: number;
	workers: number;
	band: BackfillBand;
	memory_model: BackfillMemoryModel;
};

export type BackfillProgress = {
	batch_id: string;
	state: 'queued' | 'running' | 'cancelled' | 'done';
	workers: number;
	band: BackfillBand;
	memory_model: BackfillMemoryModel;
	/** Every state's count, including the zeros. */
	counts: Record<BackfillItemState, number>;
	total: number;
	settled: number;
	created_at: string;
	updated_at: string;
	items: BackfillItem[];
};

export type BackfillBatchSummary = {
	batch_id: string;
	state: 'queued' | 'running' | 'cancelled' | 'done';
	workers: number;
	band: BackfillBand;
	created_at: string;
	note: string | null;
	counts: Record<BackfillItemState, number>;
};

export type BackfillCancelResult = { batch_id: string; cancelled: number };

export type BackfillResumeResult = {
	batch_id: string;
	requeued: number;
	workers: number;
	band: BackfillBand;
	counts: Record<BackfillItemState, number>;
};

async function _err(r: Response): Promise<never> {
	const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
	const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
	const msg = typeof body.detail === 'string' ? body.detail : detail?.message;
	throw new RbApiError(r.status, detail?.code ?? `HTTP_${r.status}`, msg ?? r.statusText);
}

/** Plan a batch. Refused tracks come back as rows with their named reason,
 * never as an absence: a smaller admitted count with no explanation is the
 * silent drop this milestone exists to remove. */
export async function enqueueBackfill(args: {
	stableIds: string[];
	lane: string;
	backend: string;
	note?: string | undefined;
}): Promise<BackfillEnqueueResult> {
	const r = await fetch(`${API_BASE}/api/v1/analysis/backfill/enqueue`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({
			stable_ids: args.stableIds,
			lane: args.lane,
			backend: args.backend,
			note: args.note ?? null
		})
	});
	if (!r.ok) await _err(r);
	return (await r.json()) as BackfillEnqueueResult;
}

export async function backfillProgress(batchId: string, limit = 200): Promise<BackfillProgress> {
	const params = new URLSearchParams({ batch_id: batchId, limit: String(limit) });
	const r = await fetch(`${API_BASE}/api/v1/analysis/backfill/progress?${params}`);
	if (!r.ok) await _err(r);
	return (await r.json()) as BackfillProgress;
}

export async function listBackfillBatches(limit = 20): Promise<BackfillBatchSummary[]> {
	const params = new URLSearchParams({ limit: String(limit) });
	const r = await fetch(`${API_BASE}/api/v1/analysis/backfill/batches?${params}`);
	if (!r.ok) await _err(r);
	return ((await r.json()) as { batches: BackfillBatchSummary[] }).batches;
}

/** Cancel pending AND in-flight items. Terminal ones are left alone. */
export async function cancelBackfill(batchId: string): Promise<BackfillCancelResult> {
	const r = await fetch(`${API_BASE}/api/v1/analysis/backfill/cancel`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ batch_id: batchId })
	});
	if (!r.ok) await _err(r);
	return (await r.json()) as BackfillCancelResult;
}

/** Put cancelled and abandoned items back; concurrency is re-planned from
 * what is LEFT, so a batch whose one long track finished does not stay at one
 * worker for its remaining club edits. */
export async function resumeBackfill(batchId: string): Promise<BackfillResumeResult> {
	const r = await fetch(`${API_BASE}/api/v1/analysis/backfill/resume`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ batch_id: batchId })
	});
	if (!r.ok) await _err(r);
	return (await r.json()) as BackfillResumeResult;
}

/** How the band names read in the UI, with the rule that produced them. */
export const BAND_LABELS: Record<BackfillBand, string> = {
	under_20_min: 'every track under 20 min: 4 workers',
	'20_to_45_min': 'longest 20 to 45 min: 2 workers',
	over_45_min: 'longest over 45 min: 1 worker',
	empty: 'nothing admitted'
};
