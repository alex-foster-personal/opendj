// PERF-GRID-03 regression lines:
// - if a deck component $derives the whole queryPerformanceState() again then broken
// - if a narrow accessor disagrees with the snapshot field it replaces then broken
// - if an expired armed countdown reads non-null through a narrow accessor then broken
// - if readTransition remaps a deck's whole beat grid on every tick then broken
//
// Measured on demon-llama, Mon 5 Oct 2026, two synced decks playing: three
// `$derived(queryPerformanceState()...)` reads (DeckHeader, Deck, WaveRow)
// re-ran on every transport tick and were 61% of main-thread time once the
// grid memo had landed.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { bundleTypeScriptModule } from './load-typescript.mjs';
import { importBundledSource } from './import-bundled-source.mjs';

const SRC = new URL('../../src/lib/', import.meta.url);
const read = (rel) => readFileSync(new URL(rel, SRC), 'utf8');

let ipc;

before(async () => {
	const text = await bundleTypeScriptModule('tests/unit/fixtures/waveform-seek-session-entry.ts');
	ipc = await importBundledSource(text, 'perf-narrow-deck-reads');
});

const BEATS = Array.from({ length: 64 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));

function clockedDriver(clock) {
	return {
		stableId: () => 'narrow-deck',
		refresh: async () => {},
		hasRbMapping: () => true,
		triggerState: () => ({ cue: null, playing: true, loopEngaged: false, positionSec: 0.2, beats: BEATS }),
		jump: async () => {},
		arm: async (_deck, _positionMs, armAt) => {
			const at = typeof armAt === 'function' ? armAt(0.2) : armAt;
			return clock.sec + (at - 0.2);
		},
		contextTimeNowSec: () => clock.sec
	};
}

async function withPlayingDeck(body) {
	globalThis.window = {};
	const originalBeatSyncMax = ipc.uiPrefs.beat_sync_max;
	ipc.uiPrefs.beat_sync_max = true;
	const deck = ipc.deckStates[1];
	Object.assign(deck, { stable_id: 'narrow-deck', playing: true, loop: null, position_ms: 200 });
	deck.anlz = { beatgrid: { source: 'rekordbox', beats: BEATS, beat_count: BEATS.length, status: 'ok' } };
	const clock = { sec: 10 };
	const resetDriver = ipc.installPerformanceHotCueDriverForTest(clockedDriver(clock));
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await body(clock);
	} finally {
		uninstall();
		resetDriver();
		Object.assign(deck, { stable_id: null, playing: false, position_ms: 0, anlz: null });
		ipc.uiPrefs.beat_sync_max = originalBeatSyncMax;
		delete globalThis.window;
	}
}

function assertParity(label) {
	const snapshot = ipc.queryPerformanceState();
	assert.equal(ipc.queryMasterMode(), snapshot.master_mode, `${label}: master_mode`);
	for (const deck of [1, 2, 3, 4]) {
		assert.deepEqual(ipc.queryWaveformSeekArmed(deck), snapshot.decks[deck].waveform_seek_armed, `${label}: seek ${deck}`);
		assert.deepEqual(
			ipc.queryQuantizedLaunchArmed(deck),
			snapshot.decks[deck].quantized_launch_armed,
			`${label}: launch ${deck}`
		);
	}
}

test('narrow reads equal the snapshot fields while nothing is armed', async () => {
	await withPlayingDeck(async () => assertParity('idle'));
});

test('a waveform seek reads the same armed and expired through the narrow accessor', async () => {
	await withPlayingDeck(async (clock) => {
		await window.musicDjToolsPerformance.dispatch({ type: 'waveform_seek', deck: 1, position_ms: 60000, snap: 'downbeat' });
		assert.equal(ipc.queryWaveformSeekArmed(1).target_position_ms, 60000);
		assertParity('seek armed');
		clock.sec += 60;
		assert.equal(ipc.queryWaveformSeekArmed(1), null, 'a landed seek reads as not armed');
		assertParity('seek expired');
	});
});

test('a quantized launch reads the same armed and expired through the narrow accessor', async () => {
	const launchClock = { sec: 10 };
	const restore = ipc.installPerformanceQuantizedLaunchDriverForTest({
		arm: async () => launchClock.sec + 0.5,
		clear: () => {},
		contextTimeNowSec: () => launchClock.sec
	});
	try {
		await withPlayingDeck(async () => {
			await window.musicDjToolsPerformance.dispatch({ type: 'play', deck: 1, playing: true, quantize: true });
			assert.ok(ipc.queryQuantizedLaunchArmed(1).remaining_ms > 0);
			assertParity('launch armed');
			launchClock.sec += 60;
			assert.equal(ipc.queryQuantizedLaunchArmed(1), null, 'a launched arm reads as not armed');
			assertParity('launch expired');
		});
	} finally {
		restore();
		ipc.resetQuantizedLaunchArmedForTest();
	}
});

test('deck components never derive the whole performance snapshot', () => {
	for (const rel of ['components/rb/deck/DeckHeader.svelte', 'components/rb/Deck.svelte', 'components/rb/wave/WaveRow.svelte']) {
		const source = read(rel);
		assert.doesNotMatch(source, /\$derived\(\s*queryPerformanceState\(\)/, `${rel} derives the whole snapshot`);
		assert.doesNotMatch(source, /=\{\s*queryPerformanceState\(\)/, `${rel} reads the whole snapshot in a template prop`);
	}
});

test('master mode is reactive state, so a narrow derived sees lock and unlock', () => {
	assert.match(read('rb/audio-engine.svelte.ts'), /let _masterMode: MasterMode = \$state\('auto'\);/);
});

test('the transition read maps each analysis grid once, not per tick', () => {
	const source = read('rb/transition-read.svelte.ts');
	assert.match(source, /beatgrid: _mappedOnce\(_beatMemo,/);
	assert.match(source, /phrases: _mappedOnce\(_phraseMemo,/);
	assert.doesNotMatch(source, /beatgrid\.beats \?\? \[\]\)\.map\(/, 'the per-tick grid copy is back');
});
