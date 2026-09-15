/**
 * PERFMODE-11: throttled `app_mode.last_gig_at` writer plus localStorage mirror
 * for synchronous cold-boot landing (no await on GET /api/v1/ui-prefs).
 */
import { writeBootStampMirror } from './gig-stamp-mirror';
import { syncDiskPrefs, type DiskPrefsPatch } from './prefs-hydrate';

export { readBootStampMirror, writeBootStampMirror } from './gig-stamp-mirror';

export const LAST_GIG_STAMP_MIN_INTERVAL_MS = 60_000;

let lastWrittenAtMs = 0;

/** Fire-and-forget: never await disk I/O before returning. */
export function touchLastGigAt(nowMs: number = Date.now()): void {
	if (nowMs - lastWrittenAtMs < LAST_GIG_STAMP_MIN_INTERVAL_MS) return;
	lastWrittenAtMs = nowMs;
	const iso = new Date(nowMs).toISOString();
	writeBootStampMirror(iso);
	const patch: DiskPrefsPatch = { app_mode: { last_gig_at: iso } };
	void syncDiskPrefs(patch);
}
