import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));

function source(relativePath) {
	return readFileSync(`${FRONTEND_ROOT}/${relativePath}`, 'utf8').replaceAll('\r\n', '\n');
}

/**
 * Coordinator regression check for the untouched /performance route.
 *
 * Regression lines:
 * - if the mixer UI seeds a separate value from the graph then the first
 *   audible track can disagree with the visible controls
 * - if graph construction stops reading mixerState then a later UI default
 *   edit can silently leave the graph on a stale initial value
 */
test('engine graph and UI seed share mixerState before the first user touch', () => {
	const state = source('src/lib/player/state.svelte.ts');
	// deck-channel-graph.ts was extracted from audio-engine.svelte.ts on
	// Sat 12 Sep 2026 (commit 1db624ef7, #2155 output-stall recovery) and now
	// owns the per-deck gain-node construction this test pins; concatenate
	// both so the extraction does not silently un-anchor this guard.
	const engine =
		source('src/lib/rb/audio-engine.svelte.ts') + '\n' + source('src/lib/rb/deck-channel-graph.ts');

	assert.match(
		state,
		/export function _defaultChannel\(deck_id: DeckId\): MixerChannelState \{[\s\S]*?trim: 0\.5,[\s\S]*?eq_high: 0\.5,[\s\S]*?eq_mid: 0\.5,[\s\S]*?eq_low: 0\.5,[\s\S]*?fader: 1/
	);
	// e0c9c32f7 (fix(webui): guard location.search when window has no location)
	// moved the seed into _createMixerState() behind a globalThis singleton, so
	// the export is no longer the $state literal itself. Pin all three links.
	assert.match(
		state,
		/function _createMixerState\(\): MixerState \{\s*const mixer = \$state\(\{[\s\S]*?crossfader: 0\.5,[\s\S]*?master: 1[\s\S]*?return mixer;/
	);
	assert.match(state, /globalRef\[MIXER_STATE_SINGLETON_KEY\] = _createMixerState\(\);/);
	assert.match(state, /export const mixerState: MixerState = _sharedMixerState\(\);/);
	assert.match(engine, /_masterGain\.gain\.value = mixerState\.master/);
	// 1db624ef7 (#2155) moved this read into deck-channel-graph.ts, which takes
	// the store through `deps`; pin both the read and that the engine hands the
	// shared mixerState in, or `deps.mixerState` could be any object.
	assert.match(engine, /const ch = deps\.mixerState\.channels\[deck\];/);
	assert.match(engine, /buildDeckChannelGraph\(\{[^}]*?\bmixerState,/);
	assert.match(engine, /trim\.gain\.value = ch\.trim \* TRIM_MAX_GAIN/);
	assert.match(engine, /low\.gain\.value = eqDbFromKnob\(ch\.eq_low\)/);
	assert.match(engine, /mid\.gain\.value = eqDbFromKnob\(ch\.eq_mid\)/);
	assert.match(engine, /high\.gain\.value = eqDbFromKnob\(ch\.eq_high\)/);
	assert.match(engine, /cue\.gain\.value = ch\.cue_enabled \? 1 : 0/);
	assert.match(engine, /fader\.gain\.value = ch\.fader/);
	// deck-channel-graph.ts calls this through a `deps.` object rather than
	// the engine's bare module-level bindings (same #2155 extraction above).
	assert.match(engine, /xfGainFor\(ch\.assign, [\w.]*mixerState\.crossfader\)/);

});
