/** FX, stem and loop/cue control ids for set-recording v2 (mirror of apps/sets/control_ids.py). */

import type { DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import type { StemControl } from '$lib/rb/stem-types';
import { STEM_CONTROL_IDS } from '$lib/rb/stem-types';

// ---------------------------------------------------------------------------
// FX family (SETREC-11827)
// ---------------------------------------------------------------------------

export const FX_COLOR_SELECT: Record<1 | 2 | 3 | 4, string> = {
	1: 'fx.1.select',
	2: 'fx.2.select',
	3: 'fx.3.select',
	4: 'fx.4.select'
};

export const FX_COLOR_WET: Record<1 | 2 | 3 | 4, string> = {
	1: 'fx.1.wet',
	2: 'fx.2.wet',
	3: 'fx.3.wet',
	4: 'fx.4.wet'
};

export const FX_COLOR_ECHO_BEATS: Record<1 | 2 | 3 | 4, string> = {
	1: 'fx.1.echo_beats',
	2: 'fx.2.echo_beats',
	3: 'fx.3.echo_beats',
	4: 'fx.4.echo_beats'
};

export const BEAT_FX_ON = 'fx.0.on';
export const BEAT_FX_SELECT = 'fx.0.select';
export const BEAT_FX_WET = 'fx.0.wet';
export const BEAT_FX_TARGET = 'fx.0.target';
export const BEAT_FX_BEATS = 'fx.0.beats';
export const BEAT_FX_QUANTIZE = 'fx.0.quantize';
export const BEAT_FX_RELEASE = 'fx.0.release';

// ---------------------------------------------------------------------------
// Stem family (SETREC-08)
// ---------------------------------------------------------------------------

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

export function hotCueId(deck: DeckId, slot: HotCueSlot): string {
	return `deck.${deck}.hot_cue.${slot}`;
}

/** Scripted stem session: mute and gain on deck 1 for every part, plus solo on vocal. */
export const SCRIPTED_STEM_TOUCH_IDS: readonly string[] = [
	...STEM_PARTS.flatMap((stem) => [stemMuteId(1, stem), stemGainId(1, stem)]),
	stemSoloId(1, 'vocal'),
	stemMuteId(2, 'vocal'),
	stemGainId(2, 'instrumental')
] as const;
