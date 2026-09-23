/**
 * Vocal state tooltip for one wavestack deck row (SPIKE-B1/B2 four mandatory
 * states). Bars are painted by render.ts for 'rekordbox' and 'demucs'; the
 * barless states get an explicit tooltip so absence is never ambiguous, and
 * demucs bars declare their non-rekordbox provenance.
 */
import { vocalsOf } from '$lib/rb/api-rb';
import type { AnlzData } from '$lib/rb/anlz-types';

export function waveRowVocalsTitle(anlz: AnlzData | null): string | null {
	if (anlz === null) return null;
	const v = vocalsOf(anlz);
	if (v.status === 'no_vocals') return 'no vocals detected';
	else if (v.status === 'not_analyzed') return 'vocals not analyzed in rekordbox';
	else if (v.status === 'demucs') return 'vocals: local detection';
	else return null; // rekordbox: the blue bars speak for themselves
}
