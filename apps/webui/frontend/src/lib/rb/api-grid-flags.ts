/**
 * Client for `/api/v1/beatgrid-flags` (GRIDFLAG-03, GRIDFLAG-04).
 *
 * The verdicts themselves arrive on the listing rows (`grid_quality`); this
 * module only asks the engine to bring them up to date and writes the
 * per-track dismissal. CLI twin: `opendj track grid-flags` / `grid-scan` / `grid-flag`.
 */
import { API_BASE } from '$lib/api';
import { bootScheduler } from '$lib/rb/boot-scheduler';

export interface GridFlagDismissResult {
	stable_id: string;
	dismissed: boolean;
	/** The track's etag after the write, for rows that keep one. */
	etag: string;
}

/** Reject with the route's own reason (FastAPI `detail`), not its raw JSON body. */
async function _fail(response: Response, what: string): Promise<never> {
	const body = await response.text();
	let reason = body;
	try {
		const detail = (JSON.parse(body) as { detail?: unknown }).detail;
		if (typeof detail === 'string') reason = detail;
		else if (detail !== undefined) reason = JSON.stringify(detail);
	} catch {
		// non-JSON body (proxy error page): keep the text as is
	}
	throw new Error(`${what} failed: HTTP ${response.status} ${reason}`);
}

/** Hide (`true`) or restore (`false`) one track's beatgrid flag. */
export async function setGridFlagDismissed(stableId: string, dismissed: boolean): Promise<GridFlagDismissResult> {
	const response = await fetch(`${API_BASE}/api/v1/beatgrid-flags/${encodeURIComponent(stableId)}/dismissed`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ dismissed })
	});
	if (!response.ok) await _fail(response, 'beatgrid flag dismiss');
	return (await response.json()) as GridFlagDismissResult;
}

let _scanRequested: Promise<void> | null = null;

/**
 * Ask the engine, once per page session, to bring stored beatgrid verdicts up
 * to date in the background. Incremental on the server (an unchanged grid is
 * one stat), and the engine publishes `library.changed` when a verdict
 * changed, which is what refreshes the rows. A failure is logged and the next
 * call retries; it never blocks the table.
 *
 * LIBM-138: the request waits for the boot window to close. The scan it starts
 * is a background thread on the same single-worker engine the listing walk is
 * using, so starting it at table mount slowed the rows it is about.
 */
export function ensureGridQualityScan(): Promise<void> {
	if (_scanRequested === null) {
		_scanRequested = new Promise<void>((resolve) => {
			bootScheduler.defer('grid-flags:scan', resolve);
		})
			.then(() => fetch(`${API_BASE}/api/v1/beatgrid-flags/scan`, { method: 'POST' }))
			.then(async (response) => {
				if (!response.ok) await _fail(response, 'beatgrid scan request');
			})
			.catch((error: unknown) => {
				_scanRequested = null;
				console.error('[grid-flags] scan request failed', error);
			});
	}
	return _scanRequested;
}
