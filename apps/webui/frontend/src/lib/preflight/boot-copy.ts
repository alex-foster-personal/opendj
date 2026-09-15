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
