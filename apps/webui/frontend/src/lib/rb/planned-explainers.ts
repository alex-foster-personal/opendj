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
	'autoplay-two-track': 'Two-track AutoPlay - will match and mix a second automatic track alongside the primary handoff, with its own evolving selection rules.',
	'grid-adjust': 'Grid adjust - nudges the beatgrid later or earlier on this deck so downbeats line up with the kick, without changing tempo.',
	'grid-shift': 'Grid shift - slides the whole beatgrid by whole beats so bar 1 lands on the phrase start you hear.',
	'auto-cue': 'Auto cue - jumps back to the loaded cue when the track ends, so the next play starts from that cue instead of 0:00.',
	'manual-source': 'Manual tempo source - ignores the analyzed BPM tag and lets you tap or type the tempo this deck should run at.',
	'loop-source': 'Loop source - picks whether loop lengths come from the beatgrid (INT), a stored hot-cue loop, or the active memory cue.',
	'hot-cue-menu': 'Hot cue bank menu - switches this pad bank between hot cues, beat loops, beat jump, and sampler, the way a performance-pad mode selector does.',
	'collection-bookmarks': 'Collection bookmarks - filters the library to tracks you have starred or tagged as favorites, without opening a playlist.',
	'explorer-grid': 'Explorer - browses the collection as folders on disk instead of playlists, so you can load from the file tree.',
	'itunes-library': 'iTunes library - shows playlists imported from Apple Music / iTunes as a source next to the rekordbox collection.',
	'file-browser': 'File browser - opens loose audio files that are not yet in the collection, for a one-off load onto a deck.',
	'beatport': 'Beatport - browses purchased or streaming Beatport tracks as a library source, with purchase links when a row is streaming-only.',
	'video-output': 'Video output - sends the master or a chosen deck to a second display or projector for visuals, separate from the headphone cue.',
	'usb-export': 'USB export - writes the current playlist to a FAT32 stick in rekordbox export layout so a CDJ can play it.',
	'cloud-lock': 'Cloud library lock - marks this collection as the rekordbox Cloud / private copy so another laptop cannot write it at the same time.',
	'play-history': 'Play history - lists tracks played this session and previous nights, with the same columns as the collection table.',
	'master-dropdown': 'Master playlist - pins which playlist the NEXT / compatible filter treats as the master source, independent of which deck is MASTER.',
	'single-column-layout': 'Single-column library - hides the playlist tree and gives the track table the full browser width.',
	'split-column-layout': 'Split-column library - stacks two track tables side by side so two playlists can be compared without switching panes.',
	'export-eject': 'Export / eject - flushes a USB export and safely ejects the stick from the bottom bar, the same action as the USB panel.',
	'pane-prev-track': 'Previous track in pane - moves the table selection up one row and, when a deck is focused, prepares that row as the next load.',
	'pane-next-track': 'Next track in pane - moves the table selection down one row and, when a deck is focused, prepares that row as the next load.',
	// Pin 552a810ba13b, second sweep (issue #4089): the remaining /performance
	// controls that still showed the bare stub.
	'mixer-knob': 'Mixer dial - a channel control (trim, EQ band, color filter, or headphone mix and level) whose audio path is not wired on this build. Once it is, drag, scroll or arrow keys turn it, double-click resets it, shift-click hands it the global scroll wheel and alt-click links it to its partner dial.',
	'crossfade-curve': 'Crossfade curve - picks how the crossfader blends the A and B buses: bass swap trades the low end between the two sides at the center point, linear is a plain level fade. Only the magic crossfader curve plays today.',
	'feedback-pin-visibility': "Other users' pins - shows the comment pins collaborators left on this screen beside your own and the agents', so shared review feedback appears in place. Needs community sharing first.",
	'context-menu-unavailable': 'Library action - this menu command will act on the selected tracks or playlist. Its handler is not connected yet, so choosing it changes nothing.',
	'track-table-filter': 'Column filter - narrows the track table to rows matching a value you pick per column (genre, key, BPM range, rating), like the rekordbox column filter funnel.',
	'quantize-grid-phase': 'Phrase quantize - snaps seeks, cue points and loop ends to the detected phrase length from analysis (for example 16 or 32 bars) instead of a fixed 1, 4 or 8 beat grid.',
	'recommended-section': 'Recommended tracks - lists library tracks that mix well out of the loaded deck, ranked by key, BPM and energy compatibility, in a strip above the table. The count is how many scored candidates are already waiting.'
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
