/**
 * Staged ingest batches awaiting manual Rekordbox import.
 */
import { getIngestPending, type PendingBatch } from './api-ingest';

const POLL_MS = 15_000;

export const ingestPending = $state({
	batches: [] as PendingBatch[],
	loading: false,
	lastError: null as string | null
});

let _pollTimer: ReturnType<typeof setInterval> | null = null;

function _startPoll(): void {
	if (_pollTimer !== null) return;
	_pollTimer = setInterval(() => {
		void refreshIngestPending();
	}, POLL_MS);
}

function _stopPoll(): void {
	if (_pollTimer !== null) {
		clearInterval(_pollTimer);
		_pollTimer = null;
	}
}

export async function refreshIngestPending(): Promise<void> {
	ingestPending.loading = true;
	try {
		const out = await getIngestPending();
		ingestPending.batches = out.batches;
		ingestPending.lastError = null;
		if (out.batches.length > 0) {
			_startPoll();
		} else {
			_stopPoll();
		}
	} catch (err) {
		ingestPending.lastError = err instanceof Error ? err.message : String(err);
	} finally {
		ingestPending.loading = false;
	}
}

export function startIngestPendingWatch(): void {
	void refreshIngestPending();
}

export function stopIngestPendingWatch(): void {
	_stopPoll();
}
