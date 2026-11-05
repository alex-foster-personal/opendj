/**
 * AGENT-24 (Codex P1 on #5822): automatic master elections hold while an agent plan owns the decks.
 *
 * [if] a plan is scheduled or running [then ⛔] a level handoff (or any non-forced automatic election) leaves the
 *   master on the plan's clock deck.
 * [if] no plan is active [then ⛔] the handoff runs as on main; a forced election (the master stopped or unloaded)
 *   runs either way.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const { automaticElectionHeld } = await loadTypeScriptModule('src/lib/rb/master-election.ts');

test('a handoff during a live plan does not move the master', () => {
	assert.equal(automaticElectionHeld({ locked: false, force: false, planOwnsDecks: true }), true);
});

test('control: with no plan the handoff runs as on main, and a lock still holds', () => {
	assert.equal(automaticElectionHeld({ locked: false, force: false, planOwnsDecks: false }), false);
	assert.equal(automaticElectionHeld({ locked: true, force: false, planOwnsDecks: false }), true);
});

test('a forced election (master stopped, played out, unloaded) runs even during a plan', () => {
	assert.equal(automaticElectionHeld({ locked: false, force: true, planOwnsDecks: true }), false);
	assert.equal(automaticElectionHeld({ locked: true, force: true, planOwnsDecks: false }), false);
});

test('every automatic election in the engine goes through the hold, with the live plan status', async () => {
	const source = await readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	const body = source.slice(
		source.indexOf('function _electPlayingMaster('),
		source.indexOf('function _electPlayingMaster(') + 400
	);
	assert.match(
		body,
		/automaticElectionHeld\(\{ locked: _masterMode === 'locked', force: options\?\.force === true, planOwnsDecks: autoPlayYieldsNow\(\) \}\)/
	);
});
