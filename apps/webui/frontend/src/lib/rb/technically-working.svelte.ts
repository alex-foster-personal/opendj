/**
 * LIBUX-05 "Technically-working mode": every /performance region becomes an
 * edge-revealed overlay so the maintainer can DJ while working elsewhere on screen.
 *
 * State only - no DOM, no keyboard, no CSS. `technically-working-hotkeys.ts`
 * drives the setters below from Ctrl+R / Opt / cmd+E and edge-hover tracking;
 * `+page.svelte` reads the visibility helpers to decide what renders.
 *
 * LIBUX-02 (hide-able Next/recommended panels, not yet built) is a narrower
 * instance of the same idea - a collapsed state that does not persist across
 * sessions. This module deliberately does not persist `active`/`peeking`/etc
 * to prefs for the same reason: session-only hide state, not a saved layout.
 *
 * Deliberately takes "is this deck loaded" as an injected predicate rather
 * than importing audio-engine.svelte.ts directly: that module pulls in
 * AudioContext/location-dependent browser globals, which would drag every
 * test of this state machine into mocking an engine it never needs to know
 * about. The caller (`+page.svelte`, which already imports the engine for
 * the decks themselves) supplies it.
 */
/**
 * The deck id, declared inline rather than imported.
 *
 * Deliberate, and the same call perf-event-log.ts and
 * presentation-clock-report.ts make for the same reason: `deck-slots.ts` is
 * the tree's fan-in ceiling and the quality ratchet holds that key at its
 * measured floor with ZERO headroom on purpose, so the next importer reds
 * the gate for every lane at once. A four-member union is not worth doing
 * that to whoever rebases next.
 */
type DeckId = 1 | 2 | 3 | 4;

export type EdgeRegion = 'top' | 'left' | 'right' | 'bottom';

/** Which decks gate a given edge's reveal - "only for the decks actually in
 * use" (LIBUX-05). `null` means the region has no deck gate (library is
 * always available; waveforms follow whichever decks are loaded overall). */
const EDGE_DECKS: Record<EdgeRegion, readonly DeckId[] | null> = {
	top: null,
	left: [1, 3],
	right: [2, 4],
	bottom: null
};

let active = $state(false);
let peeking = $state(false);
let optReveal = $state(false);
let eqRaised = $state(false);
let hoveredEdges = $state<Set<EdgeRegion>>(new Set());

export function isTechModeActive(): boolean {
	return active;
}

export function isPeeking(): boolean {
	return peeking;
}

export function isOptRevealActive(): boolean {
	return optReveal;
}

export function isEqRaised(): boolean {
	return eqRaised;
}

export function hoveredEdgeList(): EdgeRegion[] {
	return [...hoveredEdges];
}

/** Ctrl+R press: toggles overlay mode on/off and stays there (LIBUX-05:
 * "the components animate into overlay mode and stay there"). */
export function toggleTechMode(): void {
	setTechModeActive(!active);
}

export function setTechModeActive(next: boolean): void {
	if (active === next) return;
	active = next;
	if (!active) {
		hoveredEdges = new Set();
		eqRaised = false;
		optReveal = false;
	}
}

/** Ctrl+R hold: brings every region back only for as long as it is held. */
export function setPeeking(next: boolean): void {
	peeking = next;
}

/** Opt held: reveals the "no obvious home" group over a translucent backdrop. */
export function setOptReveal(next: boolean): void {
	optReveal = next && active;
}

/** cmd+E: floating EQ panel. Only meaningful in overlay mode - outside it
 * the mixer's real EQ knobs are already on screen. */
export function setEqRaised(next: boolean): void {
	eqRaised = next && active;
}

export function toggleEqRaised(): void {
	setEqRaised(!eqRaised);
}

export function setEdgeHovered(edge: EdgeRegion, hovered: boolean): void {
	if (hoveredEdges.has(edge) === hovered) return;
	const next = new Set(hoveredEdges);
	if (hovered) next.add(edge);
	else next.delete(edge);
	hoveredEdges = next;
}

function _edgeHasLoadedDeck(edge: EdgeRegion, isDeckLoaded: (deck: DeckId) => boolean): boolean {
	const decks = EDGE_DECKS[edge];
	if (decks === null) return true;
	return decks.some(isDeckLoaded);
}

/**
 * [if] the UI is in overlay mode and hidden, and the mouse moves to a given
 * edge [then] only the part belonging to that edge appears, and only for the
 * decks actually in use (LIBUX-05 acceptance bullet). `isDeckLoaded` is the
 * caller's live `getDeckState(d).stable_id !== null` check.
 */
export function isEdgeVisible(edge: EdgeRegion, isDeckLoaded: (deck: DeckId) => boolean): boolean {
	if (!active) return true;
	if (peeking) return true;
	if (!hoveredEdges.has(edge)) return false;
	return _edgeHasLoadedDeck(edge, isDeckLoaded);
}

/**
 * Per-deck resolution within an already-revealed edge. `isEdgeVisible` only
 * answers "is this edge's hover window open at all" - left/right each stack
 * two decks, and hovering must surface only the deck(s) actually loaded, not
 * the whole column just because ONE of the two has a track (LIBUX-05: "only
 * for the decks actually in use"). The same predicate also gates a deck's
 * waveform row under 'top', which has no owning edge of its own: every
 * loaded deck gets a row there, an unloaded one does not.
 */
export function isDeckSlotVisible(
	deck: DeckId,
	edge: EdgeRegion,
	isDeckLoaded: (deck: DeckId) => boolean
): boolean {
	if (!active) return true;
	if (peeking) return true;
	if (!hoveredEdges.has(edge)) return false;
	return isDeckLoaded(deck);
}

/**
 * "Controls with no obvious home" (mixer / topbar / quick-draw menu):
 * normal layout outside overlay mode, full reveal while peeking, otherwise
 * gated on Opt being held.
 */
export function isHomelessVisible(): boolean {
	if (!active) return true;
	if (peeking) return true;
	return optReveal;
}

export function resetTechnicallyWorkingStateForTests(): void {
	active = false;
	peeking = false;
	optReveal = false;
	eqRaised = false;
	hoveredEdges = new Set();
}
