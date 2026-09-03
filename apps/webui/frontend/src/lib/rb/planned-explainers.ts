/**
 * What each not-yet-built control WILL do.
 *
 * Pin 552a810ba13b (the maintainer, Wed 2 Sep 2026): "not implemented tooltips should
 * say what it is still, ideally explains what it is properly still. They're
 * part of roadmapping! We'll be adding UI ahead of builds all the time as
 * ui-contracts so we can agree the way it will look and the explainer and
 * animation are also great ways of ensuring agents are aligned with user."
 *
 * That reframes these tooltips. They are not placeholders apologising for an
 * absence - they are the UI CONTRACT, the place where the maintainer and an agent agree
 * what a control does before anyone builds it. Fourteen topbar controls shared
 * one identical string, `not implemented - see PARITY-TODO`, which says only
 * that the thing is missing: the least useful fact about a control that is
 * sitting on screen with an icon on it.
 *
 * The layout icons were copied from rekordbox (SCREENSHOT-SPEC 1), so what
 * each one is SUPPOSED to do is recoverable from the original rather than
 * invented here - which is what pin 79d3616ab3d9 asked for.
 *
 * A catalogue, not strings inline in the markup, so that the set can be
 * audited: a control with no entry throws rather than rendering an empty
 * tooltip, so adding one to the topbar forces a decision about what it means.
 */

/** Appended to every planned control, so "disabled" never reads as "broken". */
export const NOT_BUILT_MARK = 'Not built yet';

/**
 * id -> "Name - what it will do." One flat string per control rather than a
 * {name, does} pair: this catalogue is charged to the /performance bundle,
 * which is the app's tightest surface, and thirteen object wrappers plus an
 * interface bought nothing the leading "Name -" convention does not already
 * carry.
 */
export const PLANNED_CONTROLS: Record<string, string> = {
	'mode-dropdown': 'PERFORMANCE mode - switches the whole window between rekordbox-style modes - EXPORT for library prep, PERFORMANCE for playing. Only PERFORMANCE exists today, which is why it is fixed.',
	'list-view': 'List view - shows the browser as one compact line per track with no preview waveform, so more rows fit on screen. The caret picks which columns.',
	fx: 'FX panel - opens the beat-effects rack - delay, echo, reverb, roll - synced to the deck tempo. Separate from the per-channel Sound Color FX dial on the mixer.',
	'split-view': 'Split view - splits the window so the browser and the decks each get half, instead of the decks owning the top and the browser the bottom.',
	'2-deck-view': '2-deck view - hides decks 3 and 4 and gives decks 1 and 2 the full width, for a two-deck set.',
	'grid-view': 'Grid view - lays all four decks out in a 2x2 grid with equal space, rather than stacking their waveforms.',
	'4-waveform-view': '4-waveform view - stacks all four decks as full-width scrolling waveforms. This is the layout you are in now, which is why it is lit.',
	'scope-view-1': 'Phase scope - replaces the waveform stack with a circular phase display per deck, so beat alignment reads as rotation rather than as bars.',
	'scope-view-2': 'Phase meter - shows how far each follower deck is ahead of or behind the master as an arc, for correcting drift by eye.',
	link: 'LINK - joins other machines on the network - Ableton Link tempo sync, and sharing this library with a second laptop.',
	pad: 'PAD - labels the performance-pad row: hot cues, beat loops, beat jump and sampler banks for the focused deck.',
	information: 'Track information - opens the full record for the focused deck - every tag, the analysis provenance, play history and the file path.',
	'free-badge': 'Free plan - is the licence tier this copy runs on. Everything on this screen is included; the badge marks where paid tiers will differ once there are any.',
};

/** The tooltip for a planned control. Throws on an unknown id: an empty
 * tooltip on a visible control is the failure this catalogue exists to stop. */
export function plannedTitle(id: string): string {
	const entry = PLANNED_CONTROLS[id];
	if (entry === undefined) {
		throw new Error(`planned-explainers: no entry for control '${id}'`);
	}
	return `${entry} (${NOT_BUILT_MARK}.)`;
}
