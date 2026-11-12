/** @vitest-environment node */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));

test('[if] query exposes decks 1-8 [then] engine ids list eight slots', async () => {
	const { query } = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts');
	const { isShadowDeck } = await loadTypeScriptModule('src/lib/rb/deck-id.ts');
	const q = query();
	assert.equal(q.engine_decks.length, 8);
	assert.equal(q.live_decks.length, 4);
	assert.equal(q.decks.length, 8);
	for (const row of q.decks.filter((d) => isShadowDeck(d.deck))) {
		assert.equal(row.visible, false);
		assert.equal(row.loaded, false);
	}
});

test('[if] the mixer or wavestack renders [then] DECK_IDS stays 1-4 only', async () => {
	const { DECK_IDS, ENGINE_DECK_IDS, SHADOW_DECK_IDS } = await loadTypeScriptModule(
		'src/lib/player/constants.ts'
	);
	assert.deepEqual([...DECK_IDS], [1, 2, 3, 4]);
	assert.deepEqual([...ENGINE_DECK_IDS], [1, 2, 3, 4, 5, 6, 7, 8]);
	assert.deepEqual([...SHADOW_DECK_IDS], [5, 6, 7, 8]);
});

test('[if] double-click library load runs [then] picker never targets shadow slots', async () => {
	const { pickDoubleClickDeck } = await loadTypeScriptModule('src/lib/rb/deck-slots.ts');
	const decks = Object.fromEntries(
		[1, 2, 3, 4].map((id) => [
			id,
			{
				stable_id: null,
				playing: false,
				is_master: false,
				fader: 0,
				loadSeq: 0,
				reservationPending: false
			}
		])
	);
	const pick = pickDoubleClickDeck({ shift: false, replace: false, decks });
	assert.ok(pick.deck !== null && pick.deck <= 4);
});

test('[if] the mixer or wavestack renders [then] hotkeys iterate DECK_IDS not shadow ids', () => {
	const hotkeys = readFileSync(
		`${REPO_ROOT}/apps/webui/frontend/src/lib/rb/performance-hotkeys.ts`,
		'utf8'
	);
	assert.match(hotkeys, /for \(const d of DECK_IDS\)/);
	assert.doesNotMatch(hotkeys, /ENGINE_DECK_IDS/);
});
