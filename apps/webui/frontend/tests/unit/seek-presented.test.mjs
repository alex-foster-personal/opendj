// requirement: RBPERF-11950
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let seekPresented;
let perfBuckets;

before(async () => {
	seekPresented = await loadTypeScriptModule('src/lib/player/transport/seek-presented.ts');
	perfBuckets = await loadTypeScriptModule('src/lib/rb/perf-event-buckets.ts');
});

test('[if] stopped click arms seek-presented at the quantized target [then] engine records command_to_presented_ms [else stop]', () => {
	const body = engineBlockAfter(
		'async quantizedSeek(deck: DeckId, ms: number, skipGridQuantize = false, pressT0Ms?: number, jumpBeats?: number | null): Promise<void> {'
	);
	assert.match(body, /armSeekPresentedFromPressMark\(deck, targetMs\)/);
	const playhead = readFileSync(`${SRC}/lib/rb/playhead-display.svelte.ts`, 'utf8');
	assert.match(playhead, /noteSeekPresentedPosition\(deck, view\.position_ms\)/);
	const engine = readFileSync(`${SRC}/lib/rb/audio-engine.svelte.ts`, 'utf8');
	assert.match(engine, /notePositionSample\(deck, st, performance\.now\(\)\)/);
});

test('[if] drag coalescing omits the press stamp [then] intermediate seeks do not arm timing [else stop]', () => {
	const wave = readFileSync(`${SRC}/lib/components/rb/wave/wave-scrub.ts`, 'utf8');
	assert.match(wave, /request\(positionMs: number, pressT0Ms\?: number\)/);
	assert.match(wave, /await this\.dispatch\(positionMs, pressT0Ms\)/);
});

test('[if] hot-cue jump keeps pressT0Ms [then] seek-presented uses the same quantizedSeek path [else stop]', () => {
	const ipc = readFileSync(`${SRC}/lib/rb/performance-ipc.svelte.ts`, 'utf8');
	assert.match(ipc, /markSeekPresentedPress\(deck, pressT0Ms\)/);
	assert.match(ipc, /_hotCueDriver\.jump\(command\.deck, cue\.in_ms, pressT0Ms\)/);
});

test('[if] transport_pending scrub preview is local [then] published position_ms is not the intent cursor [else stop]', () => {
	const playhead = readFileSync(`${SRC}/lib/rb/playhead-display.svelte.ts`, 'utf8');
	assert.match(playhead, /playheadMs[\s\S]*published position exactly/);
	assert.match(playhead, /noteSeekPresentedPosition\(deck, view\.position_ms\)/);
});

test('[if] IPC seek uses quantizedSeek with pressT0Ms [then] presentation path matches UI [else stop]', () => {
	const ipc = readFileSync(`${SRC}/lib/rb/performance-ipc.svelte.ts`, 'utf8');
	assert.match(ipc, /markSeekPresentedPress\(command\.deck, pressT0Ms\)/);
	assert.match(
		ipc,
		/await engine\.quantizedSeek\(command\.deck, command\.position_ms, command\.exact === true, pressT0Ms\)/
	);
});

test('seek-presented completes only when presented position matches the armed target', () => {
	const {
		armSeekPresented,
		noteSeekPresentedPosition,
		presentedMatchesSeekTarget,
		seekPresentedPending
	} = seekPresented;
	const t0 = performance.now() - 50;
	armSeekPresented(1, 60_000, t0);
	assert.equal(seekPresentedPending(1), true);
	assert.equal(presentedMatchesSeekTarget(60_000.2, 60_000), true);
	assert.equal(presentedMatchesSeekTarget(61_000, 60_000), false);
	noteSeekPresentedPosition(1, 61_000);
	assert.equal(seekPresentedPending(1), true);
	noteSeekPresentedPosition(1, 60_000);
	assert.equal(seekPresentedPending(1), false);
});

test('[if] a newer seek arms on the same deck [then] the earlier target cannot complete [else stop]', () => {
	const { armSeekPresented, noteSeekPresentedPosition, seekPresentedPending } = seekPresented;
	const t0 = performance.now() - 10;
	armSeekPresented(1, 30_000, t0);
	armSeekPresented(1, 48_000, t0);
	noteSeekPresentedPosition(1, 30_000);
	assert.equal(seekPresentedPending(1), true);
	noteSeekPresentedPosition(1, 48_000);
	assert.equal(seekPresentedPending(1), false);
});

test('seek-presented rows have a dedicated ring budget', () => {
	const rows = perfBuckets.withinBudgets(
		Array.from({ length: 24 }, (_, i) => ({
			t: new Date().toISOString(),
			kind: 'seek-presented',
			deck: 1,
			message: `command_to_presented_ms=${i}`
		}))
	);
	assert.equal(rows.length, 16);
});
