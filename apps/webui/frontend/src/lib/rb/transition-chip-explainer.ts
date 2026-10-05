/**
 * Hover text for the TopBar blend pill (TRANS-02, pin 9c2caa34455f).
 * Pure: one string per classifier state, so the pill can say what it is
 * measuring instead of showing a bare word.
 */
import type { TransitionStatus } from './transition-classifier';

const STATUS_ONLY = 'Status only: it changes nothing and has no settings.';

export function transitionChipTitle(status: TransitionStatus): string {
	if (status.state === 'idle') return '';
	const outgoing = `deck ${status.outgoing_deck}`;
	const incoming = `deck ${status.incoming_deck}`;
	if (status.state === 'transitioning') {
		return (
			`Transitioning: two decks are audible on the master at once ` +
			`(${outgoing} going out, ${incoming} coming in), read from the live ` +
			`faders, crossfader and transport. ${STATUS_ONLY}`
		);
	} else if (status.state === 'approaching') {
		return (
			`Approaching: only ${outgoing} is audible, and ${incoming} looks about to ` +
			`come in (its fader or the crossfader is opening, it is synced or ` +
			`playing near its cue, or ${outgoing} is close to a phrase boundary). ` +
			`${STATUS_ONLY}`
		);
	}
	const _exhaustive: never = status.state;
	throw new Error(`Unhandled transition state: ${_exhaustive}`);
}
