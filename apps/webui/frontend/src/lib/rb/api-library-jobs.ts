/**
 * Typed client for /api/v1/library-jobs (PERFBATCH-05, issue #1865).
 * HTTP + CLI (`queue_cli user-*`) + LibraryJobQueuePanel are one triple.
 */
import { API_BASE, RbApiError } from '$lib/api';

export type LibraryJobLane = 'stems' | 'lyrics';
export type LibraryJobState =
	| 'pending'
	| 'running'
	| 'done'
	| 'skipped'
	| 'failed'
	| 'cancelled';

export type LibraryJobItem = {
	lane: LibraryJobLane;
	stable_id: string;
	state: LibraryJobState;
	reason: string | null;
	detail: string | null;
	position: number;
	attempts: number;
	enqueued_at: string;
	started_at: string | null;
	finished_at: string | null;
};

export type LibraryJobEnqueueResult = {
	lane: LibraryJobLane;
	items: LibraryJobItem[];
	already_running: string[];
};

export type LibraryJobList = {
	lane: LibraryJobLane;
	items: LibraryJobItem[];
	counts: Record<string, number>;
};

export const LIBRARY_JOB_POST_CHUNK = 500;

async function _parse<T>(res: Response, fallbackCode: string): Promise<T> {
	if (!res.ok) {
		let code = fallbackCode;
		let message = res.statusText;
		let body: unknown = null;
		try {
			body = await res.json();
			const detail = (body as { detail?: { code?: string; message?: string } }).detail;
			if (detail?.code) code = detail.code;
			if (detail?.message) message = detail.message;
		} catch {
			/* keep statusText */
		}
		throw new RbApiError(res.status, code, message, body);
	}
	return (await res.json()) as T;
}

export async function enqueueLibraryJobs(input: {
	lane: LibraryJobLane;
	stable_ids: string[];
	placement?: 'next' | 'tail';
}): Promise<LibraryJobEnqueueResult> {
	const res = await fetch(`${API_BASE}/api/v1/library-jobs/enqueue`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			lane: input.lane,
			stable_ids: input.stable_ids,
			placement: input.placement ?? 'next'
		})
	});
	return _parse<LibraryJobEnqueueResult>(res, 'enqueue_failed');
}

export async function listLibraryJobs(
	lane: LibraryJobLane,
	includeSettled = false
): Promise<LibraryJobList> {
	const q = new URLSearchParams({ lane });
	if (includeSettled) q.set('include', 'settled');
	const res = await fetch(`${API_BASE}/api/v1/library-jobs?${q}`);
	return _parse<LibraryJobList>(res, 'list_failed');
}

export async function reorderLibraryJob(
	lane: LibraryJobLane,
	stableId: string,
	beforeStableId: string | null
): Promise<LibraryJobItem> {
	const res = await fetch(`${API_BASE}/api/v1/library-jobs/${lane}/${encodeURIComponent(stableId)}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ before_stable_id: beforeStableId })
	});
	return _parse<LibraryJobItem>(res, 'reorder_failed');
}

export async function cancelLibraryJob(
	lane: LibraryJobLane,
	stableId: string
): Promise<LibraryJobItem> {
	const res = await fetch(
		`${API_BASE}/api/v1/library-jobs/${lane}/${encodeURIComponent(stableId)}/cancel`,
		{ method: 'POST' }
	);
	return _parse<LibraryJobItem>(res, 'cancel_failed');
}

/** Split a large selection so enqueue does not block the UI thread. */
export function chunkStableIds(ids: string[], size = LIBRARY_JOB_POST_CHUNK): string[][] {
	const out: string[][] = [];
	for (let i = 0; i < ids.length; i += size) out.push(ids.slice(i, i + size));
	return out;
}

export async function enqueueLibraryJobsBatched(input: {
	lane: LibraryJobLane;
	stable_ids: string[];
	placement?: 'next' | 'tail';
	yieldMs?: number;
}): Promise<LibraryJobEnqueueResult> {
	const chunks = chunkStableIds(input.stable_ids);
	const items: LibraryJobItem[] = [];
	const already: string[] = [];
	for (let i = 0; i < chunks.length; i++) {
		const part = await enqueueLibraryJobs({
			lane: input.lane,
			stable_ids: chunks[i],
			placement: input.placement ?? 'next'
		});
		items.push(...part.items);
		already.push(...part.already_running);
		if (i + 1 < chunks.length) {
			await new Promise((r) => setTimeout(r, input.yieldMs ?? 0));
		}
	}
	return { lane: input.lane, items, already_running: already };
}
