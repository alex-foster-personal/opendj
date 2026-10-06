/** Track title as the waveform gutter shows it (DECKUX-40).
 *
 * Mixed In Key writes its analysis into the title tag ("6A - 7 - Title",
 * "6A - Title", "Am - 5 - Title"). The gutter already shows the live key
 * beside the title, so the prefix is dropped from the DISPLAY only; the
 * library value and the hover title keep it. Same pattern as the server's
 * `_KEY_PREFIX` in apps/shared/artwork_sources.py, which strips it before an
 * online artwork lookup. A title that is nothing but a prefix is kept whole
 * rather than rendered blank. */
const MIK_KEY_PREFIX =
	/^\s*(?:\d{1,2}[AB]\s*-\s*(?:\d{1,2}\s*-\s*)?|[A-G][#b]?m?\s*-\s*\d{1,2}\s*-\s*)/;

export function gutterTrackTitle(title: string): string {
	const stripped = title.replace(MIK_KEY_PREFIX, '');
	return stripped.length > 0 ? stripped : title;
}
