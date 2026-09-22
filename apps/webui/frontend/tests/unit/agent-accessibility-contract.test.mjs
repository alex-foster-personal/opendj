import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

function source(relativePath) {
	const text = readFileSync(`${FRONTEND}/${relativePath}`, 'utf8');
	assert.ok(text.trim(), `if ${relativePath} is empty then this accessibility guard is inert`);
	return text;
}

test('deck controls expose scoped AX names and stable test ids', () => {
	const transport = source('src/lib/components/rb/deck/TransportCluster.svelte');
	const hotCues = source('src/lib/components/rb/deck/HotCueBank.svelte');
	const loops = source('src/lib/components/rb/deck/LoopCluster.svelte');

	assert.ok(
		transport.includes('`play deck ${deck.deck_id}`'),
		'if play names stop carrying their deck then an AX-tree agent finds four indistinguishable play buttons'
	);
	assert.ok(
		transport.includes('${QUANTIZED_LAUNCH} deck ${deck.deck_id}'),
		'if QUANTIZED LAUNCH names stop carrying their deck then an AX-tree agent finds four indistinguishable armed play buttons'
	);
	assert.ok(
		transport.includes('data-testid={`play-deck-${deck.deck_id}`}'),
		'if play loses its test id then automation has no stable deck-scoped selector'
	);
	assert.ok(
		hotCues.includes('aria-label={`hot cue ${entry.slot} deck ${deck.deck_id}`}'),
		'if hot-cue pads lose their deck-scoped names then an AX-tree agent cannot target a pad safely'
	);
	assert.ok(
		hotCues.includes('data-testid={`hot-cue-${deck.deck_id}-${entry.slot}`}'),
		'if hot-cue pads lose their test ids then automation cannot distinguish same-letter pads'
	);
	assert.ok(
		loops.includes('aria-label={`loop deck ${deckId}`}'),
		'if loop controls lose their deck-scoped name then a deck-local loop command becomes ambiguous'
	);
	assert.ok(
		loops.includes('data-testid={`loop-deck-${deckId}`}'),
		'if loop controls lose their test ids then automation cannot select the intended deck'
	);
});

test('deck and channel groups expose scoped control identity', () => {
	const deck = source('src/lib/components/rb/Deck.svelte');
	const channel = source('src/lib/components/rb/mixer/ChannelStrip.svelte');
	const knob = source('src/lib/components/rb/mixer/Knob.svelte');
	const fader = source('src/lib/components/rb/mixer/VFader.svelte');

	assert.ok(deck.includes('role="group"'), 'if a deck is not a group then its controls lose deck context');
	assert.ok(deck.includes('aria-label={`deck ${deckId}`}'), 'if a deck group loses its name then AX cannot scope its descendants');
	assert.ok(channel.includes('role="group"'), 'if a channel is not a group then mixer controls lose channel context');
	assert.ok(channel.includes('aria-label={`channel ${deckId}`}'), 'if a channel group loses its name then AX cannot scope its descendants');
	assert.ok(knob.includes('data-testid={`knob-${knobId}`}'), 'if knobs lose stable ids then same-band controls are ambiguous');
	assert.ok(fader.includes('data-testid={`channel-${deckId}-fader`}'), 'if channel faders lose stable ids then automation cannot select one channel');
});

test('MCP AX serialization includes pressed and numeric values', () => {
	const mcp = source('../../desktop/mcp/webview-mcp.ts');
	assert.ok(mcp.includes("el.getAttribute('aria-pressed')"), 'if stateOf omits aria-pressed then toggle state is invisible to MCP clients');
	assert.ok(mcp.includes("el.getAttribute('aria-valuenow')"), 'if stateOf omits aria-valuenow then slider values are invisible to MCP clients');
});

// AGENT-09 (issue #1035): the two a11y wins from closed PR #372 that never
// landed on trunk. Source-scan, matching this file's existing convention --
// no jsdom/testing-library exists in this frontend package, so structural
// position (not a rendered DOM) is what pins the regression.
test('headphone I/O button is queryable by its accessible name', () => {
	const headphones = source('src/lib/components/rb/mixer/HeadphoneCluster.svelte');
	assert.ok(
		headphones.includes('aria-label="SHOW AUDIO I/O"'),
		'if the I/O button loses its aria-label then it has no accessible name distinct from its I/O glyph text'
	);
});

test('headphone SPLIT button exposes aria-pressed for split_cable mode', () => {
	const headphones = source('src/lib/components/rb/mixer/HeadphoneCluster.svelte');
	assert.ok(
		headphones.includes('aria-pressed={headphoneState.output_mode === \'split_cable\'}'),
		'if the SPLIT button omits aria-pressed then split_cable toggle state is invisible to MCP clients'
	);
	assert.ok(
		headphones.includes('aria-label="Split cable output mode"'),
		'if the SPLIT button loses its aria-label then it has no accessible name distinct from its SPLIT glyph text'
	);
});

test('deck unload control stays reachable when artwork is missing or fails to load', () => {
	const deckHeader = source('src/lib/components/rb/deck/DeckHeader.svelte');

	const trackGateIdx = deckHeader.indexOf('{#if deck.stable_id !== null}');
	const artBtnIdx = deckHeader.indexOf('class="art-btn"');
	const artworkGateIdx = deckHeader.indexOf('{#if artSrc !== null && !artworkFailed}');
	const unloadClickIdx = deckHeader.indexOf('onclick={() => void onUnload()}');
	const imgSrcIdx = deckHeader.indexOf('src={artSrc}');

	assert.ok(
		trackGateIdx !== -1 && artBtnIdx !== -1 && artworkGateIdx !== -1,
		'if any of these markers disappear then the structure this test pins no longer exists'
	);
	assert.ok(
		trackGateIdx < artBtnIdx,
		'if the unload button is gated on artwork state again then a loaded deck with failed art has no unload control at all'
	);
	assert.ok(
		artBtnIdx < artworkGateIdx,
		'if artSrc/artworkFailed gate the whole button instead of just the img inside it then unload is unreachable whenever art fails'
	);
	assert.ok(
		unloadClickIdx > artBtnIdx && unloadClickIdx < artworkGateIdx,
		'if onUnload stops firing outside the artwork-present branch then a failed-art deck cannot be unloaded'
	);
	assert.ok(
		imgSrcIdx > artworkGateIdx,
		'if the real artwork <img> is removed then a present-artwork deck no longer renders its art unchanged'
	);
});
