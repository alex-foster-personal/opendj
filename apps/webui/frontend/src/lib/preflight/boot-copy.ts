/**
 * Boot-gate copy helpers (issue #2722). Pure functions so heading escalation
 * is unit-testable without mounting PreflightScreen.
 */
import type { PreflightCheck } from '../api';

export const BOOT_ESCALATION_FAIL_POLLS = 3;

export const BOOT_WELCOME_HEADING = "Let's get your music in";
export const BOOT_BLOCKED_HEADING = "Let's get your music in";
export const BOOT_ESCALATED_HEADING = 'This needs your action';
export const BOOT_STARTING_HEADING = 'Starting up';

export function libraryAttachedStatus(
	checks: readonly PreflightCheck[]
): PreflightCheck['status'] | 'unknown' {
	const row = checks.find((check) => check.id === 'library-attached');
	return row?.status ?? 'unknown';
}

export function needsImportAction(checks: readonly PreflightCheck[]): boolean {
	const status = libraryAttachedStatus(checks);
	return status === 'fail' || status === 'pending';
}

/** Issue #2653: the gate cleared (overall pass) but the library is still
 * empty-dismissed (or otherwise not attached) -- setup has not completed. */
export function setupStillPending(cleared: boolean, checks: readonly PreflightCheck[]): boolean {
	return cleared && needsImportAction(checks);
}

/** Issue #2653: whether the root layout mounts the non-blocking preflight
 * strip. Before #2653 a cleared gate always unmounted it, taking the
 * dismissed-empty row's Run setup control with it. */
export function shouldShowPreflightStrip(
	blockOnPreflight: boolean,
	cleared: boolean,
	checks: readonly PreflightCheck[]
): boolean {
	if (blockOnPreflight) return false;
	return !cleared || needsImportAction(checks);
}

/** Heading for the boot gate surface. */
export function bootGateHeading(
	blocking: boolean,
	checks: readonly PreflightCheck[],
	consecutiveFailPolls: number
): string {
	if (!blocking) return 'Startup checks';
	const libraryStatus = libraryAttachedStatus(checks);
	if (libraryStatus === 'fail') {
		if (consecutiveFailPolls >= BOOT_ESCALATION_FAIL_POLLS) {
			return BOOT_ESCALATED_HEADING;
		}
		return BOOT_BLOCKED_HEADING;
	}
	if (consecutiveFailPolls >= BOOT_ESCALATION_FAIL_POLLS) {
		return BOOT_ESCALATED_HEADING;
	}
	return BOOT_STARTING_HEADING;
}
