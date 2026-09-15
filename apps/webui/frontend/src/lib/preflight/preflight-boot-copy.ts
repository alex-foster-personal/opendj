/**
 * Plain-language labels and details for PREFLIGHT-01's boot gate (issue #2722).
 *
 * Admin mode keeps the server's technical strings; boot mode maps them here
 * so a 0-track user never sees schema versions or subsystem names.
 */
import type { PreflightCheck } from '../api';

const LIBRARY_ATTACHED_CHECK_ID = 'library-attached';

const BOOT_LABELS: Record<string, string> = {
	'engine-alive': 'Starting up',
	'state-db': 'Your library',
	'library-attached': 'Music library',
	'audio-access': 'Audio permission'
};

/** User-facing row title in boot mode. Admin mode uses the server label. */
export function bootCheckLabel(check: PreflightCheck, mode: 'boot' | 'admin'): string {
	if (mode === 'admin') return check.label;
	return BOOT_LABELS[check.id] ?? check.label;
}

/** User-facing detail in boot mode. Admin mode uses the server detail verbatim. */
export function bootCheckDetail(check: PreflightCheck, mode: 'boot' | 'admin'): string {
	if (mode === 'admin') return check.detail;
	if (check.id === 'engine-alive') return 'Connected to the app';
	if (check.id === 'state-db') {
		if (check.detail.includes('no state.db yet')) return 'No library imported yet';
		if (check.detail.includes('no tracks table yet')) return 'Library is empty';
		if (check.detail.includes('schema_meta')) {
			return 'Your library database needs an update before the app can start.';
		}
	}
	if (check.id === LIBRARY_ATTACHED_CHECK_ID) {
		if (check.detail.includes('no state.db')) return 'No music imported yet';
		if (check.detail.includes('0 tracks')) return 'Your library has no tracks yet';
		if (check.detail.includes('dismissed')) return 'Setup was skipped with an empty library';
	}
	if (check.id === 'audio-access') {
		if (check.detail.includes('no state.db yet')) return 'Nothing to check until music is imported';
		if (check.status === 'pass') return 'Can read audio files';
	}
	return scrubBootDetail(check.detail);
}

/** Strip schema-version jargon that should never appear on the boot screen. */
export function scrubBootDetail(detail: string): string {
	if (/schema_meta/i.test(detail)) {
		return 'Your library database needs an update before the app can start.';
	}
	return detail.replace(/schema_meta\s+version\s+\d+/gi, 'database version');
}

/** Boot-screen headline before escalation. */
export const BOOT_HEADLINE_STARTING = 'Starting up';
/** Boot-screen headline after repeated fail polls (P1-4). */
export const BOOT_HEADLINE_NEEDS_ACTION = 'This needs your action';
