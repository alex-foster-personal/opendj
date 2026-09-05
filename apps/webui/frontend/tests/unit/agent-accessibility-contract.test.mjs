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
		transport.includes("aria-label={`play deck ${deck.deck_id}`}"),
		'if play names stop carrying their deck then an AX-tree agent finds four indistinguishable play buttons'
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
