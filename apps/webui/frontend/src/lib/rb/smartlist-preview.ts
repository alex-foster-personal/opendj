/** Library-browser smartlist preview cap (issue #2452). */

export const SMARTLIST_PREVIEW_CAP = 2000;

export function smartlistMatchIsTruncated(loadedCount: number, matchTotal: number): boolean {
	return matchTotal > loadedCount;
}

export function smartlistTruncationNote(loaded: number, total: number): string {
	const n = loaded.toLocaleString('en-US');
	const m = total.toLocaleString('en-US');
	return `Showing first ${n} of ${m} matching tracks`;
}
