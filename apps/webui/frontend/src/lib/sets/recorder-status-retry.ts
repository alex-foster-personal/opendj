/** Recorder status reads that got no answer (OPEN-DJ-BE-4G).
 *
 * The /performance REC rail and the /sets panel poll `getRecorderStatus()`.
 * When the local daemon restarts or is briefly busy, `fetch` itself rejects.
 * The provenance is the call: `rethrowSetsError` turns every HTTP answer into
 * a plain `Error`, so a `TypeError` out of `getRecorderStatus()` means nothing
 * answered. WebKit words it `Load failed (127.0.0.1:7700)`, so this checks the
 * type, not the text.
 *
 * One missed read is not a fault: the next read usually lands. The caller
 * retries with backoff, keeping the last status, and raises one grouped error
 * toast (still reported, with the cause) once the daemon has stayed silent
 * for `REC_STATUS_UNREACHABLE_LIMIT` reads in a row.
 */

export const REC_STATUS_UNREACHABLE_LIMIT = 3;
export const REC_STATUS_UNREACHABLE_GROUP = 'rec-status-unreachable';

const RETRY_BASE_MS = 2000;
const RETRY_MAX_MS = 8000;

/** True when the status read got no answer at all (fetch rejected). */
export function recorderStatusUnreachable(error: unknown): boolean {
	return error instanceof TypeError;
}

/** Delay before the next read after `failures` unanswered reads in a row. */
export function recorderStatusRetryMs(failures: number): number {
	const n = Math.max(1, failures);
	return Math.min(RETRY_BASE_MS * 2 ** (n - 1), RETRY_MAX_MS);
}

/** The toast for a daemon that has stayed silent: says what is wrong, keeps
 * the runtime's own text for the report. */
export function recorderStatusUnreachableMessage(failures: number, error: unknown): string {
	return `REC status: the engine is not answering (${failures} reads in a row): ${String(error)}`;
}
