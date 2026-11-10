/** Stem control ids for set-recording v2 (mirror of apps/sets/control_ids.py). */

import type { DeckId } from '$lib/rb/deck-slots';
import type { StemControl } from '$lib/rb/stem-types';
import { STEM_CONTROL_IDS } from '$lib/rb/stem-types';

export const STEM_PARTS = STEM_CONTROL_IDS;

export function stemGainId(deck: DeckId, stem: StemControl): string {
	return `deck.${deck}.stem.${stem}.gain`;
}

export function stemMuteId(deck: DeckId, stem: StemControl): string {
	return `deck.${deck}.stem.${stem}.mute`;
}

export function stemSoloId(deck: DeckId, stem: StemControl): string {
	return `deck.${deck}.stem.${stem}.solo`;
}

/** Scripted stem session: mute and gain on deck 1 for every part, plus solo on vocal. */
export const SCRIPTED_STEM_TOUCH_IDS: readonly string[] = [
	...STEM_PARTS.flatMap((stem) => [stemMuteId(1, stem), stemGainId(1, stem)]),
	stemSoloId(1, 'vocal'),
	stemMuteId(2, 'vocal'),
	stemGainId(2, 'instrumental')
] as const;
