/**
 * requirement: LYR-14
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

// LYR-14 (the maintainer, Wed 7 Oct 2026: "Use design C, but put hot cues dropdown (not
// functional yet) below the Top and Bottom grid adjust"): the deck lyric strip
// runs UNDER the hot-cue bank at its full width, and the HOT CUE pad-mode menu
// lives in the left grid column under the two grid buttons.

function _rule(css, selector) {
	const at = css.indexOf(`${selector} {`);
	assert.ok(at >= 0, `missing CSS rule ${selector}`);
	return css.slice(at, css.indexOf('}', at));
}

test('DeckLyricLine renders under HotCueBank inside a column cue-flex', async () => {
	const deck = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const cueOpen = deck.indexOf('class="cue-flex"');
	const loopOpen = deck.indexOf('class="loop-col"');
	const bankPos = deck.indexOf('<HotCueBank', cueOpen);
	const lyricPos = deck.indexOf('<DeckLyricLine', cueOpen);
	assert.ok(cueOpen >= 0 && loopOpen > cueOpen, 'cue-flex region is present');
	assert.ok(bankPos > cueOpen && bankPos < loopOpen, 'HotCueBank lives in cue-flex');
	assert.ok(lyricPos > bankPos && lyricPos < loopOpen, 'DeckLyricLine comes after the bank, inside cue-flex');
	const style = deck.slice(deck.indexOf('<style>'));
	const cueFlex = _rule(style, '\t.cue-flex');
	assert.match(cueFlex, /flex-direction: column;/, 'the bank and the strip stack vertically');
	const bank = _rule(style, '\t.cue-flex > :global(.cue-area)');
	assert.match(bank, /align-self: stretch;/, 'the bank keeps the full column width');
	// The pads never shrink below their content and are never clipped
	// (ui-border-audit caught 0.5-1px pad borders clipped by overflow).
	assert.match(bank, /flex: 1 0 auto;/, 'the bank never shrinks below its pads');
	assert.doesNotMatch(bank, /overflow: hidden/, 'the bank must not clip its pad borders');
	const host = _rule(style, '\t.deck-lyric-host');
	assert.match(host, /flex: 0 1 auto;/, 'the strip is the side that gives a pixel');
	assert.match(host, /min-height: 0;/);
	assert.match(deck, /class="deck-lyric-host" style:height=\{`\$\{deckLyricStripHeight\}px`\}/);
});

// Codex P2 on PR #5847: HotCueBank's UNDO (after a save, rename or delete)
// stacked under the pads and left the lyric strip ~12px. It must float out of
// the column flow, and the strip must give up only width while it is up.
test('UNDO floats at the cue column corner and narrows the lyric strip instead of starving it', async () => {
	const deck = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const style = deck.slice(deck.indexOf('<style>'));
	assert.match(_rule(style, '\t.cue-flex'), /position: relative;/, 'the column anchors the floating UNDO');
	const undo = _rule(style, '\t.cue-flex > :global(.cue-area > .undo)');
	assert.match(undo, /position: absolute;/, 'UNDO leaves the vertical flow');
	assert.match(undo, /right: 0;/);
	assert.match(undo, /bottom: 0;/);
	const narrowed = _rule(style, '\t.cue-flex:has(> :global(.cue-area > .undo)) > .deck-lyric-host');
	assert.match(narrowed, /padding-right: calc\(var\(--deck-undo-w, 56px\) \+ 4px\);/, 'the strip yields the UNDO width');
	assert.doesNotMatch(narrowed, /height/, 'the strip keeps its row height while UNDO is up');
	// The bank still renders UNDO inside its own root, which these selectors target.
	const bank = await readFile('src/lib/components/rb/deck/HotCueBank.svelte', 'utf8');
	assert.match(bank, /<button class="rb-lit-button undo"/);
});

test('the HOT CUE pad-mode menu sits stacked under the grid buttons, not in the bank', async () => {
	const deck = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const bank = await readFile('src/lib/components/rb/deck/HotCueBank.svelte', 'utf8');
	// GRIDEDIT-6235: the grid adjust/shift pair is its own component now, so the
	// column holds that component and then the menu.
	const gridOpen = deck.indexOf('<div class="left-edge">');
	assert.ok(gridOpen >= 0, 'the left-edge grid column exists');
	const gridClose = deck.indexOf('</div>', gridOpen);
	const gridBlock = deck.slice(gridOpen, gridClose);
	const shift = gridBlock.indexOf('<GridEditButtons');
	const menu = gridBlock.indexOf('<PadModeMenu {deckId} stacked />');
	assert.ok(shift >= 0, 'grid shift button is in the grid column');
	assert.ok(menu > shift, 'the HOT CUE menu comes after (below) the grid shift button');
	const buttons = await readFile('src/lib/components/rb/deck/GridEditButtons.svelte', 'utf8');
	assert.ok(
		buttons.indexOf('grid-adjust-deck-') < buttons.indexOf('grid-shift-deck-'),
		'grid adjust sits above grid shift'
	);
	assert.equal(deck.split('<PadModeMenu').length - 1, 1, 'Deck mounts exactly one pad-mode menu');
	assert.doesNotMatch(bank, /<PadModeMenu/, 'HotCueBank no longer mounts the menu');
});

test('the stacked trigger wraps HOT over CUE without changing its text', async () => {
	const menu = await readFile('src/lib/components/rb/deck/PadModeMenu.svelte', 'utf8');
	assert.match(menu, /class:stacked/);
	const style = menu.slice(menu.indexOf('<style>'));
	const stacked = _rule(style, '\t.dropdown.stacked');
	assert.match(stacked, /width: min-content;/, 'min-content wraps the label at its space');
	assert.match(stacked, /height: auto;/, 'two lines need more than the 18px single-line height');
	// The label text is unchanged by stacking: the trigger prints the current pad mode's
	// label upper-cased (PR #9956), HOT CUE by default, so the accessible name and the caret test hold.
	assert.match(menu, />\s*\{triggerLabel\}\s*<\/button>/);
	assert.match(menu, /\?\? 'Hot Cue'\)\.toUpperCase\(\)/, 'with no mode picked the trigger reads HOT CUE');
});
