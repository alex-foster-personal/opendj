/** localStorage mirror for PERFMODE-11 synchronous cold-boot stamp reads. */

export const APP_MODE_LAST_GIG_MIRROR_KEY = 'app_mode_last_gig_at';
export const PREFS_STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

export function readBootStampMirror(): string | null {
	try {
		const raw = localStorage.getItem(PREFS_STORAGE_KEY);
		if (raw === null) return null;
		const parsed = JSON.parse(raw) as Record<string, unknown>;
		const stamp = parsed[APP_MODE_LAST_GIG_MIRROR_KEY];
		return typeof stamp === 'string' ? stamp : null;
	} catch {
		return null;
	}
}

export function writeBootStampMirror(iso: string): void {
	try {
		const raw = localStorage.getItem(PREFS_STORAGE_KEY);
		const parsed = raw === null ? {} : (JSON.parse(raw) as Record<string, unknown>);
		parsed[APP_MODE_LAST_GIG_MIRROR_KEY] = iso;
		localStorage.setItem(PREFS_STORAGE_KEY, JSON.stringify(parsed));
	} catch {
		/* quota / private mode */
	}
}
