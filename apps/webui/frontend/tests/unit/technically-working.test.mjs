/**
 * LIBUX-05 "Technically-working mode" state module.
 *
 * Regression lines:
 * - if setTechModeActive(true) makes an edge visible before it is hovered
 *   then the overlay never actually hides anything
 * - if hovering an edge with no loaded deck still reveals it then "only for
 *   the decks actually in use" (LIBUX-05 acceptance bullet) breaks
 * - if peeking does not force every edge/homeless region visible then
 *   holding cmd+R stops bringing the whole UI back
 * - if turning tech mode off leaves a stale hovered-edge/eq/opt flag set
 *   then re-entering overlay mode starts in a state nothing chose
 * - if isHomelessVisible ignores optReveal while active then Opt-held has
 *   nothing to reveal
 * - if setOptReveal(true) can activate while tech mode is off then Opt does
 *   something outside the feature it belongs to
 * - if hovering left with only deck 1 loaded also reveals deck 3's empty
 *   panel then "only for the decks actually in use" breaks per-deck, not
 *   just per-edge
 * - if hovering top with only deck 2 loaded reveals every waveform row then
 *   the same bug hits the row that has no owning edge of its own
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

/** Fake "which decks are loaded" predicate this module was built to take as
 * an injected argument rather than importing the audio engine itself. */
function loadedPredicate(loadedDecks) {
	const set = new Set(loadedDecks);
	return (deck) => set.has(deck);
}
const NONE_LOADED = loadedPredicate([]);

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/technically-working.svelte.ts');
});

beforeEach(() => {
	mod.resetTechnicallyWorkingStateForTests();
});

test('outside overlay mode every region reads visible regardless of hover/peek/opt', () => {
	assert.equal(mod.isTechModeActive(), false);
	for (const edge of ['top', 'left', 'right', 'bottom']) {
		assert.equal(mod.isEdgeVisible(edge, NONE_LOADED), true);
	}
	assert.equal(mod.isHomelessVisible(), true);
});

test('entering overlay mode hides every region until its edge is hovered', () => {
	mod.setTechModeActive(true);
	for (const edge of ['top', 'left', 'right', 'bottom']) {
		assert.equal(mod.isEdgeVisible(edge, NONE_LOADED), false, `${edge} must start hidden`);
	}
	assert.equal(mod.isHomelessVisible(), false);
});

test('hovering an edge with no loaded deck does not reveal it (left/right are deck-gated)', () => {
	mod.setTechModeActive(true);
	mod.setEdgeHovered('left', true);
	assert.equal(mod.isEdgeVisible('left', NONE_LOADED), false, 'no deck loaded - hover must not reveal');
	assert.equal(
		mod.isEdgeVisible('left', loadedPredicate([1])),
		true,
		'deck 1 now loaded - the same hover now reveals'
	);
});

test('top and bottom edges are not deck-gated (waveforms/library always answer a hover)', () => {
	mod.setTechModeActive(true);
	mod.setEdgeHovered('top', true);
	mod.setEdgeHovered('bottom', true);
	assert.equal(mod.isEdgeVisible('top', NONE_LOADED), true);
	assert.equal(mod.isEdgeVisible('bottom', NONE_LOADED), true);
	assert.equal(mod.isEdgeVisible('left', NONE_LOADED), false, 'unhovered/ungated edges stay hidden');
});

test('unhovering an edge hides it again', () => {
	mod.setTechModeActive(true);
	const loaded = loadedPredicate([2]);
	mod.setEdgeHovered('right', true);
	assert.equal(mod.isEdgeVisible('right', loaded), true);
	mod.setEdgeHovered('right', false);
	assert.equal(mod.isEdgeVisible('right', loaded), false);
});

test('peeking reveals every edge and the homeless group regardless of hover/opt', () => {
	mod.setTechModeActive(true);
	mod.setPeeking(true);
	for (const edge of ['top', 'left', 'right', 'bottom']) {
		assert.equal(mod.isEdgeVisible(edge, NONE_LOADED), true, `${edge} must be visible while peeking`);
	}
	assert.equal(mod.isHomelessVisible(), true);
	mod.setPeeking(false);
	assert.equal(mod.isEdgeVisible('top', NONE_LOADED), false, 'releasing the peek re-hides');
});

test('Opt reveal does nothing outside overlay mode and does not leak into the next session', () => {
	// Outside overlay mode the homeless group is always visible anyway (the
	// normal, un-hidden layout), so the only way to prove setOptReveal(true)
	// was a no-op here is that it has NOT armed a stale flag for later.
	mod.setOptReveal(true);
	mod.setTechModeActive(true);
	assert.equal(
		mod.isHomelessVisible(),
		false,
		'Opt held before overlay mode was entered must not carry a reveal into it'
	);
	mod.setOptReveal(true);
	assert.equal(mod.isHomelessVisible(), true);
	mod.setOptReveal(false);
	assert.equal(mod.isHomelessVisible(), false);
});

test('toggleTechMode flips active and clears hover/eq/opt state on exit', () => {
	mod.toggleTechMode();
	assert.equal(mod.isTechModeActive(), true);
	mod.setEdgeHovered('top', true);
	mod.setOptReveal(true);
	mod.setEqRaised(true);
	assert.equal(mod.isEqRaised(), true);
	mod.toggleTechMode();
	assert.equal(mod.isTechModeActive(), false);
	assert.deepEqual(mod.hoveredEdgeList(), []);
	assert.equal(mod.isEqRaised(), false);
	// Re-entering starts clean, not with the previous session's reveals.
	mod.setTechModeActive(true);
	assert.equal(mod.isEdgeVisible('top', NONE_LOADED), false);
});

test('isDeckSlotVisible resolves per-deck, not per-edge: a loaded column-mate does not drag along an empty one', () => {
	mod.setTechModeActive(true);
	mod.setEdgeHovered('left', true);
	const loaded = loadedPredicate([1]);
	assert.equal(mod.isDeckSlotVisible(1, 'left', loaded), true, 'deck 1 is loaded - its panel shows');
	assert.equal(
		mod.isDeckSlotVisible(3, 'left', loaded),
		false,
		"deck 3 is not loaded - hovering left must not reveal its empty panel just because deck 1's edge is open"
	);
});

test('isDeckSlotVisible gates the top edge per waveform row even though isEdgeVisible does not gate it at all', () => {
	mod.setTechModeActive(true);
	mod.setEdgeHovered('top', true);
	const loaded = loadedPredicate([2]);
	assert.equal(mod.isDeckSlotVisible(2, 'top', loaded), true, 'deck 2 is loaded - its waveform row shows');
	for (const deck of [1, 3, 4]) {
		assert.equal(
			mod.isDeckSlotVisible(deck, 'top', loaded),
			false,
			`deck ${deck} is not loaded - its waveform row must stay hidden even though isEdgeVisible('top') is unconditionally true`
		);
	}
});

test('isDeckSlotVisible ignores loaded state outside overlay mode and while peeking', () => {
	const noneLoaded = NONE_LOADED;
	assert.equal(mod.isDeckSlotVisible(1, 'left', noneLoaded), true, 'tech mode off - normal layout, no gating');
	mod.setTechModeActive(true);
	mod.setPeeking(true);
	assert.equal(mod.isDeckSlotVisible(1, 'left', noneLoaded), true, 'peeking forces every deck back regardless of loaded state');
});

test('eq raise only takes effect while overlay mode is active, and toggles', () => {
	mod.setEqRaised(true);
	assert.equal(mod.isEqRaised(), false, 'tech mode is off - cmd+E has nothing to raise');
	mod.setTechModeActive(true);
	mod.toggleEqRaised();
	assert.equal(mod.isEqRaised(), true);
	mod.toggleEqRaised();
	assert.equal(mod.isEqRaised(), false);
});

test('edge hover uses the typed performance dispatcher instead of bypassing its lifecycle lock', async () => {
	const hotkeys = await readFile('src/lib/rb/technically-working-hotkeys.ts', 'utf8');
	const dispatcher = await readFile('src/lib/rb/performance-ipc.svelte.ts', 'utf8');
	assert.match(hotkeys, /runPerformanceCommandFromUi\(\{ type: 'tech_mode_edge_hover', edge, hovered:/);
	assert.match(dispatcher, /\| \{ type: 'tech_mode_edge_hover'; edge: EdgeRegion; hovered: boolean \}/);
	assert.match(dispatcher, /setEdgeHovered\(command\.edge, command\.hovered\)/);
});
