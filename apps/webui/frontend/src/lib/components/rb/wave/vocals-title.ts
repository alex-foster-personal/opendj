/**
 * The ONE vocal-state tooltip (SPIKE-B1/B2 four mandatory states; VOX-TIP-01).
 * Every surface that draws vocal bars (wavestack WaveRow, deck StripWaveform,
 * browser PreviewStrip) imports this, so the wording cannot drift between
 * copies again. Bars are painted for 'rekordbox' and 'demucs'; the barless
 * states get an explicit tooltip so absence is never ambiguous.
 *
 * Wording (the maintainer, Tue 6 Oct 2026: "shouldn't say not analyzed in rbx just
 * 'vocals not found/analyzed'"): no tooltip names rekordbox. A source label
 * appears only when there are regions to attribute, and only for local
 * detection, whose bars are not rekordbox's.
 */
import { vocalsOf, type Vocals } from '$lib/rb/api-rb';
import type { AnlzData } from '$lib/rb/anlz-types';

export const VOCALS_NOT_ANALYZED_TITLE = 'vocals not analyzed yet';
export const VOCALS_NONE_FOUND_TITLE = 'no vocals found';
export const VOCALS_LOCAL_DETECTION_TITLE = 'vocals: local detection';

/** Tooltip for one validated vocals payload; null when the bars speak for themselves. */
export function vocalsStatusTitle(vocals: Vocals | null): string | null {
	if (vocals === null) return null;
	switch (vocals.status) {
		case 'not_analyzed':
			return VOCALS_NOT_ANALYZED_TITLE;
		case 'no_vocals':
			return VOCALS_NONE_FOUND_TITLE;
		case 'demucs':
			return vocals.regions.length > 0 ? VOCALS_LOCAL_DETECTION_TITLE : VOCALS_NONE_FOUND_TITLE;
		case 'rekordbox':
			return vocals.regions.length > 0 ? null : VOCALS_NONE_FOUND_TITLE;
		default: {
			const _exhaustive: never = vocals;
			throw new Error(
				`vocalsStatusTitle: unknown status ${String((_exhaustive as { status?: unknown }).status)}`
			);
		}
	}
}

export function waveRowVocalsTitle(anlz: AnlzData | null): string | null {
	if (anlz === null) return null;
	return vocalsStatusTitle(vocalsOf(anlz));
}
