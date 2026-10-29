/**
 * The table's background verdict scans, in one place (GRIDFLAG-02, QLTSCAN-03).
 *
 * Both scans are stored server-side and incremental; the table asks once per
 * page session for each to be brought up to date, after the boot window.
 * The per-track grid dismissal is re-exported so the table reads both from
 * this one module.
 */
import { audioQualityHover, ensureAudioQualityScan } from '$lib/rb/api-audio-quality';
import { ensureGridQualityScan } from '$lib/rb/api-grid-flags';

export { setGridFlagDismissed } from '$lib/rb/api-grid-flags';
export { audioQualityHover };

/** Start (or reuse) the beatgrid and audio-quality scan requests. */
export function ensureLibraryScans(): void {
	void ensureGridQualityScan();
	// Retries itself; a final failure is already logged, so it is not rethrown here.
	ensureAudioQualityScan().catch(() => undefined);
}
