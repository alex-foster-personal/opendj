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
	fx: 'FX panel - opens the beat FX unit: CH SELECT 1 to 4 or master, effect, beat division with its time in ms, level, ON/OFF, quantize and X-PAD. It opens once the fx.beat_rack flag is on.',
	'split-view': 'Split view - splits the window so the browser and the decks each get half, instead of the decks owning the top and the browser the bottom.',
	'2-deck-view': '2-deck view - hides decks 3 and 4 and gives decks 1 and 2 the full width, for a two-deck set.',
	'grid-view': 'Grid view - lays all four decks out in a 2x2 grid with equal space, rather than stacking their waveforms.',
	link: 'LINK - joins other machines on the network - Ableton Link tempo sync, and sharing this library with a second laptop.',
	'free-badge': 'Free plan - is the licence tier this copy runs on. Everything on this screen is included; the badge marks where paid tiers will differ once there are any.',
	'hot-cue-menu': 'Hot cue bank menu - switches this pad bank between hot cues, beat loops, beat jump, and sampler, the way a performance-pad mode selector does.',
	'collection-bookmarks': 'Collection bookmarks - filters the library to tracks you have starred or tagged as favorites, without opening a playlist.',
	'itunes-library': 'iTunes library - shows playlists imported from Apple Music / iTunes as a source next to the rekordbox collection.',
	'file-browser': 'File browser - opens loose audio files that are not yet in the collection, for a one-off load onto a deck.',
	'beatport': 'Beatport - browses purchased or streaming Beatport tracks as a library source, with purchase links when a row is streaming-only.',
	'video-output': 'Video output - sends the master or a chosen deck to a second display or projector for visuals, separate from the headphone cue.',
	'cloud-lock': 'Cloud library lock - marks this collection as the rekordbox Cloud / private copy so another laptop cannot write it at the same time.',
	'master-dropdown': 'Master playlist - pins which playlist the NEXT / compatible filter treats as the master source, independent of which deck is MASTER.',
	// Pin 552a810ba13b, second sweep (issue #4089): the remaining /performance
	// controls that still showed the bare stub.
	'mixer-knob': 'Mixer dial - a channel control (trim, EQ band, color filter, or headphone mix and level) whose audio path is not wired on this build. Once it is, drag, scroll or arrow keys turn it, double-click resets it, shift-click hands it the global scroll wheel and alt-click links it to its partner dial.',
	'crossfade-curve': 'Crossfade curve - picks how the crossfader blends the A and B buses: bass swap trades the low end between the two sides at the center point, linear is a plain level fade. Only the magic crossfader curve plays today.',
	'feedback-pin-visibility': "Other users' pins - shows the comment pins collaborators left on this screen beside your own and the agents', so shared review feedback appears in place. Needs community sharing first.",
	'context-menu-unavailable': 'Library action - this menu command will act on the selected tracks or playlist. Its handler is not connected yet, so choosing it changes nothing.',
	'track-table-filter': 'Column filter - will gather every active column filter in one place, like the rekordbox Track Filter bar. The BPM range, the keys, the genres and the minimum rating have their own funnels on the BPM, Key, Genre and Rating headers, and My Tags on the Tags header.',
	'pad-mode-unbuilt': 'Pad mode - this performance-pad mode (for example slicer, sampler or keyboard) will turn the eight pads into its own controls. It is listed so the menu matches the hardware, but it is not built yet, so picking it changes nothing.',
};

/**
 * TOPVIEW-01..07 (Fri 9 Oct 2026): top-bar controls that were planned here and
 * are now built. Same "Name - what it does" shape, minus the not-built line,
 * so the explainer a user saw while it was planned keeps describing it.
 */
export const BUILT_CONTROLS: Record<string, string> = {
	'list-view': 'List view - hides the scrolling waveforms so the library below gets their height and more track rows fit on screen. The decks and mixer stay where they are. Click the 4-waveform icon to bring the waveforms back.',
	'4-waveform-view': '4-waveform view - stacks every deck as a full-width scrolling waveform. Lit while it is the view; click it to come back from list view or a phase view.',
	'scope-view-1': 'Phase scope - replaces the waveform stack with one dial per deck whose dot turns once per bar, so decks in phase turn together and a drifting deck leads or lags.',
	'scope-view-2': 'Phase meter - replaces the waveform stack with one arc per deck showing how far its beat is ahead of (right) or behind (left) the master deck, in ms, for correcting drift by eye.',
	information: 'Track information - opens the full record for the focused deck (the deck you last clicked, else the master): tags, analysis provenance, play counts and the file path.',
	// BEATFX-14: the built FX panel (fx.beat_rack on); `fx` above stays the flag-off explainer.
	'fx-panel': 'FX panel - opens the beat FX unit, laid out like a DJM Beat FX section: CH SELECT 1 to 4 or M, effect, BEAT back and forward with the time in ms at the tempo, LEVEL/DEPTH, ON/OFF, QUANTIZE and an X-PAD you touch to play the effect while held. Separate from the Sound Color FX dial on the mixer.',
	'pane-prev-track': 'Previous track in pane - moves the table selection up one row and scrolls it into view; with nothing selected it selects the last row. Agents: opendj browser_prev_track.',
	'pane-next-track': 'Next track in pane - moves the table selection down one row and scrolls it into view; with nothing selected it selects the first row. Agents: opendj browser_next_track.',
	'single-column-layout': 'Single-column library - hides the playlist tree and gives the track table the full browser width.',
	'split-column-layout': 'Split-column library - stacks two track tables side by side so two playlists can be compared without switching panes. Click a table to make it the active pane; drag rows onto the other table to add them to its playlist. Agents: opendj library_split_column.',
	'autoplay-two-track': 'Two-track AutoPlay - AutoPlay plays the set on decks 1 and 2 only, two tracks at a time: each handoff loads the other of the two, and decks 3 and 4 stay yours for samples or a manual load. Off: AutoPlay may use any free deck when the partner deck is busy.'
};

/** The tooltip for a built control: its whole entry. Throws on an unknown id. */
export function builtTitle(id: string): string {
	const entry = BUILT_CONTROLS[id];
	if (entry === undefined) {
		throw new Error(`planned-explainers: no built entry for control '${id}'`);
	}
	return entry;
}

/** Rich ControlExplainer bullets for a built topbar control. Throws on an unknown id. */
export function builtExplainerBullets(id: string): readonly string[] {
	const entry = BUILT_CONTROLS[id];
	if (entry === undefined) {
		throw new Error(`planned-explainers: no built entry for control '${id}'`);
	}
	const dash = entry.indexOf(' - ');
	return [dash === -1 ? entry : entry.slice(dash + 3)];
}

/** The tooltip for a planned control. Throws on an unknown id: an empty
 * tooltip on a visible control is the failure this catalogue exists to stop. */
export function plannedTitle(id: string): string {
	const entry = PLANNED_CONTROLS[id];
	if (entry === undefined) {
		throw new Error(`planned-explainers: no entry for control '${id}'`);
	}
	return `${entry} (${NOT_BUILT_MARK}.)`;
}

/** Rich ControlExplainer bullets for a planned topbar control (what + not built). */
export function plannedExplainerBullets(id: string): readonly string[] {
	const entry = PLANNED_CONTROLS[id];
	if (entry === undefined) {
		throw new Error(`planned-explainers: no entry for control '${id}'`);
	}
	const dash = entry.indexOf(' - ');
	const description = dash === -1 ? entry : entry.slice(dash + 3);
	return [description, `${NOT_BUILT_MARK}. See PARITY-TODO for build order.`];
}
