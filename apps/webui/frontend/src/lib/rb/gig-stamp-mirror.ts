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

/**
 * Mirror the stamp INTO the existing prefs blob. With no blob yet, write
 * nothing: a blob holding only the stamp is one prefs.svelte.ts `_load`
 * rejects as malformed (no `hide_broken_links`), so the next page load of a
 * fresh profile threw before the app mounted. Found by the Electron shell
 * gate (ELECTRON-6208): a new Electron profile over an engine that already
 * holds a gig stamp, reloaded once. The prefs blob's own first save comes
 * soon after, and the next stamp write mirrors into it.
 */
export function writeBootStampMirror(iso: string): void {
	try {
		const raw = localStorage.getItem(PREFS_STORAGE_KEY);
		if (raw === null) return;
		const parsed = JSON.parse(raw) as Record<string, unknown>;
		parsed[APP_MODE_LAST_GIG_MIRROR_KEY] = iso;
		localStorage.setItem(PREFS_STORAGE_KEY, JSON.stringify(parsed));
	} catch {
		/* quota / private mode */
	}
}
