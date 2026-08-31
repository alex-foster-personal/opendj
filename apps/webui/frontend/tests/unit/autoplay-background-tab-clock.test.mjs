import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * AutoPlay must keep handing off while the tab is in the BACKGROUND.
 *
 * deckStates[deck].position_ms is published from the engine's
 * requestAnimationFrame tick, and the browser stops rAF entirely for a hidden
 * tab. AutoPlay used to trigger off that mirror, so a backgrounded tab froze
 * the position, remaining time never crossed AUTO_PLAY_THRESHOLD_MS, and the
 * set stopped dead at the end of the playing track. The poll itself was never
 * the problem: it is a setInterval, and a tab playing audio keeps those
 * running (throttled to roughly 1s, leaving ~16 chances inside the 16s
 * window).
 *
 * [if] _snaps reads d.position_ms again [then] AutoPlay stalls the moment the
 *   user tabs away, and no other test in the suite notices - broken.
 * [if] the audio-clock read is published back into DeckState [then] the UI
 *   position jumps ahead of what is audible by the output latency - broken.
 * [if] a paused deck reports a clock position [then] a stopped deck drifts
 *   against its own cursor - broken.
 */

let audio;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: 'https://autoplay-clock.example.test'
	});
});

describe('deckAudioClockPositionMs', () => {
	it('is exported for decision-makers that cannot rely on rAF', () => {
		assert.equal(typeof audio.deckAudioClockPositionMs, 'function');
	});

	it('reports the published cursor for a deck that is not playing', () => {
		for (const deck of audio.DECK_IDS) {
			const state = audio.getDeckState(deck);
			assert.equal(state.playing, false);
			assert.equal(audio.deckAudioClockPositionMs(deck), state.position_ms);
		}
	});

	it('reads the clock while playing, and never writes DeckState', () => {
		const body = engineBlockAfter(
			'export function deckAudioClockPositionMs(deck: DeckId): number {'
		);
		assert.match(body, /_currentPosSec\(deck\) \* 1000/, 'must read the control clock');
		assert.match(body, /if \(!st\.playing\) return st\.position_ms;/, 'paused decks use the cursor');
		assert.doesNotMatch(
			body,
			/st\.\w+\s*=/,
			'this is a read for decisions only; publishing it would put the UI ahead of the audio'
		);
	});
});

describe('AutoPlay triggers off the audio clock, not the rAF mirror', () => {
	const SOURCE = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);

	it('_snaps takes position from the clock reader', () => {
		assert.match(SOURCE, /position_ms: deckAudioClockPositionMs\(id\),/);
	});

	it('_snaps never falls back to the rAF-published mirror', () => {
		const snaps = SOURCE.slice(
			SOURCE.indexOf('function _snaps()'),
			SOURCE.indexOf('function _excludeIds(')
		);
		assert.ok(snaps.length > 0, 'if _snaps cannot be located then this guard asserts nothing');
		assert.doesNotMatch(
			snaps,
			/position_ms: d\.position_ms/,
			'reading the rAF mirror here is exactly the background-tab stall'
		);
	});

	it('the trigger threshold leaves room for a throttled background poll', async () => {
		const { AUTO_PLAY_THRESHOLD_MS } = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
		// A hidden tab that is playing audio keeps setInterval running but
		// throttles it to roughly one call per second. The window has to be wide
		// enough that the handoff still gets several attempts at that cadence.
		const BACKGROUND_POLL_FLOOR_MS = 1000;
		const attempts = Math.floor(AUTO_PLAY_THRESHOLD_MS / BACKGROUND_POLL_FLOOR_MS);
		assert.ok(
			attempts >= 4,
			`threshold ${AUTO_PLAY_THRESHOLD_MS}ms gives only ${attempts} throttled attempt(s)`
		);
		// The declared poll must also be at least as fast as that floor, or the
		// foreground cadence is the binding constraint instead.
		const controller = readFileSync(
			fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
			'utf8'
		);
		const poll = /const POLL_MS = (\d+);/.exec(controller);
		assert.notEqual(poll, null, 'if POLL_MS cannot be read then this guard asserts nothing');
		assert.ok(Number(poll[1]) <= BACKGROUND_POLL_FLOOR_MS);
	});
});
