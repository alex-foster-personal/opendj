/**
 * SINGLE point of coupling between the wavestack build unit and the
 * audio-engine build unit (built in parallel - COMPONENT-MAP 1.6).
 *
 * Expected module: src/lib/rb/audio-engine.svelte.ts (rune store - the .svelte.ts
 * extension is REQUIRED, see RECON-FRONTEND 10.1) with exports:
 *   engine: AudioEngine                      - contract in $lib/rb/types
 *   getDeckState(deck: DeckId): DeckState    - stable live reactive object
 *
 * If the audio-engine unit shipped different names or a different path,
 * fix THIS FILE ONLY - WaveRow.svelte imports the engine exclusively
 * from here.
 */
export { engine, getDeckState, DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
