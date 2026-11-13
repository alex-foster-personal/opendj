/**
 * Keyboard selector ids for the channel strip buttons (GitHub issue #13696, epic #9372).
 *
 * The cue button, the colour FX slot and the R/M calibration buttons sit under the
 * channel group (`mixer.ch<n>`) beside trim, EQ, filter and fader. Ids are plain strings
 * composed from `selId.channel`, so `selector-register.ts` gains no new helper.
 */
import { selId } from '$lib/keyboard/selector-register';
import type { DeckId } from '$lib/rb/deck-slots';

export type ChannelButtonSelectorIds = {
	cue: string;
	fx: string;
	cal: string;
	calRed: string;
	calCeiling: string;
};

/** Returns the selector ids of channel n's cue leaf, colour FX group and calibration group with its two leaves. */
export function channelButtonSelectorIds(n: DeckId): ChannelButtonSelectorIds {
	const channel = selId.channel(n);
	return {
		cue: `${channel}.cue`,
		fx: `${channel}.fx`,
		cal: `${channel}.cal`,
		calRed: `${channel}.cal.red`,
		calCeiling: `${channel}.cal.ceiling`
	};
}
