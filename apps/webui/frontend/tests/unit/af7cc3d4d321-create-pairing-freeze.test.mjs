/**
 * Pin af7cc3d4d321 (issue #4083): Create pairing UI freezes the open moment.
 *
 * [if] pairing_snapshot_open runs [then] live deck motion does not mutate the snapshot [else stop].
 * [if] beat_sync_max is on [then] timestamps use beats [else stop].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pairing;

before(async () => {
	pairing = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
		viteApiBase: 'https://pairing.example.test'
	});
});

const DRIFTING_GRID = {
	beatgrid: {
		beats: [
			{ n: 1, bpm: 127, t: 0.135 },
			{ n: 2, bpm: 127, t: 0.608 },
			{ n: 3, bpm: 127, t: 1.08 },
			{ n: 4, bpm: 127, t: 1.553 },
			{ n: 1, bpm: 126, t: 2.04 }
		]
	}
};

test('af7cc3d4d321 pairing snapshot stays frozen after live deck and EQ changes', async () => {
	globalThis.window = {};
	const uninstall = pairing.installPerformanceBrowserIpc();
	try {
		pairing.uiPrefs.beat_sync_max = false;
		for (const deckId of [1, 2]) {
			const deck = pairing.deckStates[deckId];
			deck.stable_id = `track-${deckId}`;
			deck.title = `Track ${deckId}`;
			deck.position_ms = deckId * 1000;
			deck.anlz = null;
			Object.assign(pairing.mixerState.channels[deckId], { eq_low: 0.2, eq_mid: 0.5, eq_high: 0.8 });
		}
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_open' });
		const frozenPosition = pairing.queryPerformanceState().pairing_snapshot.decks[0].position_ms;
		pairing.deckStates[1].position_ms = 99999;
		pairing.mixerState.channels[1].eq_low = 0.95;
		const afterLive = pairing.queryPerformanceState().pairing_snapshot.decks[0];
		assert.equal(afterLive.position_ms, frozenPosition);
		assert.equal(afterLive.eq_adjusts.length, 2);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('af7cc3d4d321 beat_sync_max captures beat-number timestamps at open', async () => {
	globalThis.window = {};
	const uninstall = pairing.installPerformanceBrowserIpc();
	try {
		pairing.uiPrefs.beat_sync_max = true;
		for (const deckId of [1, 2, 3, 4]) {
			const deck = pairing.deckStates[deckId];
			deck.stable_id = null;
			deck.title = null;
			deck.position_ms = 0;
			deck.anlz = null;
		}
		const deck = pairing.deckStates[1];
		deck.stable_id = 'grid-track';
		deck.title = 'Grid Track';
		deck.position_ms = 700;
		deck.anlz = DRIFTING_GRID;
		await pairing.dispatchPerformanceCommand({ type: 'pairing_snapshot_open' });
		const ts = pairing.queryPerformanceState().pairing_snapshot.decks[0].timestamp;
		assert.equal(ts.unit, 'beats');
		assert.equal(ts.value, 2);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});
