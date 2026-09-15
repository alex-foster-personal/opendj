/**
 * First-run orchestration helpers (issue #2722). Pure predicates extracted
 * from +layout.svelte so the preflight-empty-library path is unit-testable.
 */
import type { PreflightCheck } from '../api';

export const ENGINE_ALIVE_CHECK_ID = 'engine-alive';
export const LIBRARY_ATTACHED_CHECK_ID = 'library-attached';

function checkStatus(
	checks: readonly PreflightCheck[],
	id: string
): PreflightCheck['status'] | 'unknown' {
	return checks.find((check) => check.id === id)?.status ?? 'unknown';
}

/** True when the engine is healthy but the library row says import is needed. */
export function needsSetupForEmptyLibrary(
	checks: readonly PreflightCheck[],
	setupOpen: boolean
): boolean {
	if (setupOpen) return false;
	if (checkStatus(checks, ENGINE_ALIVE_CHECK_ID) !== 'pass') return false;
	const library = checkStatus(checks, LIBRARY_ATTACHED_CHECK_ID);
	return library === 'fail' || library === 'pending';
}
