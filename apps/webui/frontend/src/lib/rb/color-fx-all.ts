/**
 * COLORFX-13: fan one `color_fx {deck: 'all', effect}` out to four per-deck
 * commands (rekordbox 7.2.8 single-mode USER, DJM-A9 manual p. 39). Each channel
 * follows COLORFX-8341 independently.
 */
import { DECK_IDS, type DeckId } from '$lib/player/constants';
import {
	assertColorFxAllCommand,
	type ColorFxCommand,
	type ColorFxEffect,
	type ColorFxSwitchPlan,
	planColorFxSwitch
} from './color-fx';

export interface ColorFxChannelNow {
	effect: ColorFxEffect;
	knob: number;
}

/** Fan one `deck: 'all'` command out to the four per-deck commands it stands for. */
export function colorFxAllCommands(
	command: ColorFxCommand
): Record<DeckId, ColorFxCommand & { deck: DeckId }> {
	assertColorFxAllCommand(command);
	const effect = command.effect!;
	const out = {} as Record<DeckId, ColorFxCommand & { deck: DeckId }>;
	for (const d of DECK_IDS) {
		out[d] = {
			type: 'color_fx',
			deck: d,
			effect,
			...(command.echo_beats !== undefined ? { echo_beats: command.echo_beats } : {})
		};
	}
	return out;
}

/** Each channel's own switch plan (COLORFX-8341 per channel). */
export function planColorFxAll(
	now: Record<DeckId, ColorFxChannelNow>,
	command: ColorFxCommand
): Record<DeckId, ColorFxSwitchPlan> {
	const perDeck = colorFxAllCommands(command);
	const plans = {} as Record<DeckId, ColorFxSwitchPlan>;
	for (const d of DECK_IDS) {
		const ch = now[d];
		plans[d] = planColorFxSwitch(ch.effect, ch.knob, perDeck[d]);
	}
	return plans;
}
