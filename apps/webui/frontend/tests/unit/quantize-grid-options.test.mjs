import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Pin a67bafbfc4b0 - Quantize grid options (JogDial.svelte .rb-lit-button Q
 * control): the shared hover/focus explainer (ControlExplainer) gains
 * selectable 1/4/8 beat grid options plus the 'phase' option, which
 * DECKCTL-6235c (alpha status I13, Fri 9 Oct 2026) built: a pause, seek or cue
 * snaps to the nearest phrase boundary, loops keep whole beats (see
 * deck-controls-p2.test.mjs for the math). The main Q button label
 * mirrors the active grid (Q1/Q4/Q8/Q-phase). 2-beat is omitted: the
 * underlying QuantizeGrid setting has no 2 value to select.
 *
 * Regression lines:
 * - if quantize_grid stops validating beats against 1|4|8|'phase' then broken
 * - if beats:'phase' does not reach the deck read model then the phrase
 *   option is a dead button
 * - if beats:1|4|8 does not update deckStates[deck].quantize_grid_beats then
 *   selecting a real option has no effect
 * - if JogDial renders a second explainer component instead of reusing
 *   ControlExplainer then broken
 * - if the phase option goes back to rb-inert or loses its title then broken
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

test('quantize_grid beats:1/4/8 and phase update the deck read model', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 8 });
		assert.equal(ipc.queryPerformanceState().decks[1].quantize_grid_beats, 8);

		await window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 4 });
		assert.equal(ipc.queryPerformanceState().decks[1].quantize_grid_beats, 4);

		// DECKCTL-6235c: phase is a real choice now, and back to a beat grid works.
		await window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 'phase' });
		assert.equal(ipc.queryPerformanceState().decks[1].quantize_grid_beats, 'phase');
		await window.musicDjToolsPerformance.dispatch({ type: 'quantize_grid', deck: 1, beats: 4 });
		assert.equal(ipc.queryPerformanceState().decks[1].quantize_grid_beats, 4);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('JogDial reuses the shared ControlExplainer, offers 1/4/8 and phase as real options, and labels Q by the active grid', () => {
	assert.match(JOG_DIAL, /import ControlExplainer from '\.\/ControlExplainer\.svelte';/);
	assert.match(
		JOG_DIAL,
		/<ControlExplainer title=\{qTitle\} bullets=\{qGridBullets\} tooltip=\{TOOLTIP_ISOLATED\}>/,
		'must reuse the shared explainer, not add a second one'
	);
	assert.match(JOG_DIAL, /\{#snippet action\(\)\}/);
	assert.match(JOG_DIAL, /\[1, 4, 8\] as const as beats \(beats\)/, 'no 2-beat option: no underlying support');
	assert.doesNotMatch(JOG_DIAL, /q-grid-opt[^"]*"[^{]*>\s*2\s*</, 'a literal 2-beat option must never appear');

	// DECKCTL-6235c: the phase option is real - live, titled, and it dispatches.
	assert.doesNotMatch(JOG_DIAL, /class="q-grid-opt rb-inert"/);
	assert.match(JOG_DIAL, /title=\{phraseTitle\}/);
	assert.match(JOG_DIAL, /data-testid=\{`quantize-grid-phase-deck-\$\{deck\.deck_id\}`\}/);
	assert.match(JOG_DIAL, /onclick=\{async \(\) => await onQuantizeGrid\('phase'\)\}/);

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
	// DECKCTL-6235c: pause and cue snap through snappedPositionMs (the deck's own
	// grid, phrase included); seek goes through gridSeekDecisionMs, which picks the
	// phrase decision for 'phase' and the beat-grid decision otherwise.
	const callSites = [
		/snappedPositionMs\(pauseBeats, st\.anlz\?\.phrases, positionSec \* 1000, st\.quantize_grid_beats\)/,
		// Pin 334a50710ef0 defect A: this call site moved into
		// quantizedSeekDecisionMs (loops.ts) so the deck's coarser grid can be
		// skipped for a beat-jump loop shift; gridSeekDecisionMs threads the
		// deck's own grid into it (checked below) rather than a hardcoded default.
		/gridSeekDecisionMs\(seekBeats, st\.anlz\?\.phrases, ms, st\.quantize_grid_beats, skipGridQuantize, st\.loop\)/,
		/snappedPositionMs\(cueBeats, st\.anlz\?\.phrases, st\.position_ms, st\.quantize_grid_beats\)/,
		/resolvedLoopState\([\s\S]{0,160}loopBeats !== null && beatLength === null && quantize \? loopGridBeats\(st\.quantize_grid_beats\) : null/
	];
	for (const pattern of callSites) {
		assert.match(AUDIO_ENGINE, pattern, `missing grid-aware call: ${pattern}`);
	}
	// A loop never snaps to a phrase: the loop grid maps 'phase' to whole beats.
	assert.match(LOOPS, /export function loopGridBeats\(grid: QuantizeGrid\): 1 \| 4 \| 8 \{\s*return grid === 'phase' \? 1 : grid;/);
	// gridSeekDecisionMs hands 1/4/8 to quantizedSeekDecisionMs with the grid it was given.
	assert.match(
		LOOPS,
		/: quantizedSeekDecisionMs\(beats, ms, grid, skipGridQuantize, loop\);/
	);
	// quantizedSeekDecisionMs (the moved seek call site) must actually pass
	// gridBeats into the snap, not a hardcoded 1.
	assert.match(LOOPS, /quantizeToNearestGridBeat\(beats, ms \/ 1000, gridBeats\)/);
	assert.match(LOOPS, /quantizedLoopEndpointsMs\(beats, loop, true, gridBeats\)/);
});
