/**
 * Client for `/api/v1/audio-quality/scan` (QLTSCAN-03, QLTSCAN-05).
 *
 * The letters themselves arrive on the listing rows (`quality.grade`); this
 * module only asks the engine to bring the stored facts up to date. CLI twin:
 * `opendj track quality-scan`.
 */
import { API_BASE } from '$lib/api/base';
import { requestCoalescer, type Coalescer } from '$lib/api/request-coalescer';
import { bootScheduler } from '$lib/rb/boot-scheduler';

let _scanRequested: Promise<void> | null = null;
let _scanAttempts = 0;
/** A failed request is retried this many times, this far apart, then reported. */
const SCAN_RETRY_LIMIT = 3;
const SCAN_RETRY_DELAY_MS = 30_000;

export const HOVER_TTL_MS = 30_000;

/**
 * Ask the engine, once per page session, to scan the library's audio in the
 * background. Incremental and resumable on the server (an unchanged file is
 * one stat; a stopped scan picks up where it left off), one background-priority
 * worker, and the engine publishes `library.changed` when letters changed,
 * which is what refreshes the rows. Like the beatgrid scan, it waits for the
 * boot window. A failed request is retried up to SCAN_RETRY_LIMIT times, 30 s
 * apart; after that the returned promise rejects with the last error, so the
 * failure is never reported as a started scan. It never blocks the table.
 */
export function ensureAudioQualityScan(): Promise<void> {
	if (_scanRequested === null) {
		_scanRequested = new Promise<void>((resolve) => {
			bootScheduler.defer('audio-quality:scan', resolve);
		})
			.then(() => fetch(`${API_BASE}/api/v1/audio-quality/scan`, { method: 'POST' }))
			.then(async (response) => {
				if (!response.ok) throw new Error(`audio-quality scan request failed: HTTP ${response.status} ${await response.text()}`);
				_scanAttempts = 0;
			})
			.catch((error: unknown) => {
				_scanAttempts += 1;
				console.error(`[audio-quality] scan request failed (attempt ${_scanAttempts} of ${SCAN_RETRY_LIMIT})`, error);
				if (_scanAttempts < SCAN_RETRY_LIMIT) {
					_scanRequested = null;
					setTimeout(() => void ensureAudioQualityScan().catch(() => undefined), SCAN_RETRY_DELAY_MS);
				}
				throw error;
			});
	}
	return _scanRequested;
}

/**
 * The full QLT hover for one track, from `GET /api/v1/audio-quality/{id}`.
 * Reused for 30 s per track and grade (`gradeKey`, so a row whose grade
 * changed asks again). A failed read is worded, never blank.
 */
export function audioQualityHover(
	stableId: string,
	gradeKey: string,
	coalescer: Coalescer = requestCoalescer
): Promise<string> {
	const key = `${stableId}|${gradeKey}`;
	return coalescer
		.share(key, HOVER_TTL_MS, () =>
			fetch(`${API_BASE}/api/v1/audio-quality/${encodeURIComponent(stableId)}`).then(async (response) => {
				if (!response.ok) throw new Error(`HTTP ${response.status} ${await response.text()}`);
				const body = (await response.json()) as { quality: { grade: { hover: string } } };
				return body.quality.grade.hover;
			})
		)
		.catch((error: unknown) => {
			return `Audio quality details could not be loaded: ${error instanceof Error ? error.message : String(error)}`;
		});
}
