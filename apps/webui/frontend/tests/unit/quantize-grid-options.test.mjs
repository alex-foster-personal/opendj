import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Pin a67bafbfc4b0 - Quantize grid options (JogDial.svelte .rb-lit-button Q
 * control): the shared hover/focus explainer (ControlExplainer) gains
 * selectable 1/4/8 beat grid options plus an explicitly NOT implemented
 * "match to phase length" ('phase') option - greyed rb-inert, its own
 * explainer text, and a performance-bus command that rejects with
 * not_implemented before it can reach the engine. The main Q button label
 * mirrors the active grid (Q1/Q4/Q8/Q-phase). 2-beat is omitted: the
 * underlying QuantizeGrid setting has no 2 value to select.
 *
 * Regression lines:
 * - if quantize_grid stops validating beats against 1|4|8|'phase' then broken
 * - if beats:'phase' reaches engine.setQuantizeGrid instead of being
 *   rejected in _dispatchUnknown then the not_implemented contract is broken
 * - if beats:1|4|8 does not update deckStates[deck].quantize_grid_beats then
 *   selecting a real option has no effect
 * - if JogDial renders a second explainer component instead of reusing
 *   ControlExplainer then broken
 * - if the phase option loses its rb-inert styling or explainer text then
 *   broken
 * - if the Q label stops mirroring quantize_grid_beats then broken
 * - if a "2" option appears anywhere then broken (no underlying support)
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const JOG_DIAL = readFileSync(`${SRC}/lib/components/rb/deck/JogDial.svelte`, 'utf8');
const AUDIO_ENGINE = readFileSync(`${SRC}/lib/rb/audio-engine.svelte.ts`, 'utf8');
const LOOPS = readFileSync(`${SRC}/lib/player/transport/loops.ts`, 'utf8');

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

test('quantize_grid validates beats against 1/4/8/phase and rejects anything else through the real IPC dispatch', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 2 }),
			/beats must be 1, 4, 8, or "phase"/
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 4, extra: true }),
			/unexpected fields/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('quantize_grid beats:1/4/8 update the deck read model; phase is rejected before it can queue', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 8 });
		assert.equal(ipc.queryPerformanceState().decks[1].quantize_grid_beats, 8);

		await window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 4 });
		assert.equal(ipc.queryPerformanceState().decks[1].quantize_grid_beats, 4);

		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 'phase' }),
			/not_implemented - will match quantize to the detected phase length/i
		);
		// The rejection must not have silently applied 'phase' anyway.
		assert.equal(ipc.queryPerformanceState().decks[1].quantize_grid_beats, 4);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('JogDial reuses the shared ControlExplainer, offers 1/4/8 real options plus an inert phase option, and labels Q by the active grid', () => {
	assert.match(JOG_DIAL, /import ControlExplainer from '\.\/ControlExplainer\.svelte';/);
	assert.match(
		JOG_DIAL,
		/<ControlExplainer title=\{qTitle\} bullets=\{qGridBullets\}>/,
		'must reuse the shared explainer, not add a second one'
	);
	assert.match(JOG_DIAL, /\{#snippet action\(\)\}/);
	assert.match(JOG_DIAL, /\[1, 4, 8\] as const as beats \(beats\)/, 'no 2-beat option: no underlying support');
	assert.doesNotMatch(JOG_DIAL, /q-grid-opt[^"]*"[^{]*>\s*2\s*</, 'a literal 2-beat option must never appear');

	// The phase option: explicitly plumbed but NOT implemented.
	assert.match(JOG_DIAL, /class="q-grid-opt rb-inert"/);
	// Pin 552a810ba13b: its tooltip says what phrase quantize IS, from the catalog.
	assert.match(JOG_DIAL, /title=\{plannedTitle\('quantize-grid-phase'\)\}/);
	assert.match(JOG_DIAL, /data-testid=\{`quantize-grid-phase-deck-\$\{deck\.deck_id\}`\}/);
	// It must be disabled and call nothing - never onQuantizeGrid('phase' as any).
	assert.doesNotMatch(JOG_DIAL, /onQuantizeGrid\('phase'\)/);
	assert.doesNotMatch(JOG_DIAL, /onQuantizeGrid\(beats\)[\s\S]{0,80}phase/);

	// Real options call the real handler with the real beat count.
	assert.match(JOG_DIAL, /onclick=\{async \(\) => await onQuantizeGrid\(beats\)\}/);

	// Q label mirrors the active grid.
	assert.match(
		JOG_DIAL,
		/const qLabel: string = \$derived\(\s*deck\.quantize_grid_beats === 'phase' \? 'Q-phase' : `Q\$\{deck\.quantize_grid_beats\}`\s*\);/
	);
	assert.match(JOG_DIAL, /\{qLabel\}\s*<\/button>\s*<\/ControlExplainer>/);
});

test('every quantized seek/cue/pause/loop call site threads the deck\'s own selected grid, not a hardcoded default', () => {
	// Bot review P1 (pin a67bafbfc4b0 follow-up): quantize_grid_beats was
	// stored but never read by any snap calculation. The pure-function tests
	// prove quantizedPositionMs/quantizedLoopEndpointsMs behave correctly
	// GIVEN a gridBeats argument; this proves every real call site actually
	// supplies one instead of relying on the 1-only default.
	const callSites = [
		/quantizedPositionMs\(pauseBeats, positionSec \* 1000, true, _quantizeGridBeats\(st\)\)/,
		// Pin 334a50710ef0 defect A: this call site moved into
		// quantizedSeekDecisionMs (loops.ts) so the deck's coarser grid can be
		// skipped for a beat-jump loop shift; checked below that it still
		// threads _quantizeGridBeats(st) rather than a hardcoded default.
		/quantizedSeekDecisionMs\(seekBeats, ms, _quantizeGridBeats\(st\), skipGridQuantize, st\.loop\)/,
		/quantizedPositionMs\(cueBeats, st\.position_ms, true, _quantizeGridBeats\(st\)\)/,
		/resolvedLoopState\([\s\S]{0,160}loopBeats !== null && beatLength === null \? _quantizeGridBeats\(st\) : null/
	];
	for (const pattern of callSites) {
		assert.match(AUDIO_ENGINE, pattern, `missing grid-aware call: ${pattern}`);
	}
	// The helper itself must refuse 'phase' rather than let it reach a snap.
	assert.match(
		AUDIO_ENGINE,
		/function _quantizeGridBeats\(st: DeckState\): 1 \| 4 \| 8 \{[\s\S]{0,200}beats === 'phase'/
	);
	// quantizedSeekDecisionMs (the moved seek call site) must actually pass
	// gridBeats into the snap, not a hardcoded 1.
	assert.match(LOOPS, /quantizeToNearestGridBeat\(beats, ms \/ 1000, gridBeats\)/);
	assert.match(LOOPS, /quantizedLoopEndpointsMs\(beats, loop, true, gridBeats\)/);
});
