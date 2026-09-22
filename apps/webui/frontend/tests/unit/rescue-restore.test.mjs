import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// requirement: RESCUE-04
// [if] newest snapshot is 3 h old [then] toast is "Restored layout from 3 h ago"
// [if] layout-only restore runs [then] dispatch log contains no play: true
// [if] malformed rescue blob [then] parse returns null

async function _loadRescueSnapshot() {
	return loadTypeScriptModule('src/lib/rb/rescue-snapshot.ts');
}

async function _loadRescueRestore() {
	return loadTypeScriptModule('src/lib/rb/rescue-restore.svelte.ts');
}

async function _loadRescuePlay() {
	return loadTypeScriptModule('src/lib/rb/performance-rescue-play.ts');
}

test('formatRescueAgeToast reports 3 h ago', async () => {
	const mod = await _loadRescueRestore();
	const nowMs = 1_700_000_000_000;
	const captured = nowMs - 3 * 60 * 60 * 1000;
	assert.equal(mod.formatRescueAgeToast(captured, nowMs), 'Restored layout from 3 h ago');
});

test('planRescueSimultaneousPlay shares one context time using beat_phase anchors', async () => {
	const mod = await _loadRescuePlay();
	const schedule = mod.planRescueSimultaneousPlay(
		[
			{ deck: 1, beat_phase_ms: 12_000 },
			{ deck: 3, beat_phase_ms: 45_000 }
		],
		10,
		0.05
	);
	assert.equal(schedule.length, 2);
	assert.equal(schedule[0].startAtContextTime, 10.05);
	assert.equal(schedule[1].startAtContextTime, 10.05);
});

// Superseded by the real four-deck session/layout/play restore regression in
// tests/e2e/performance-mixtour-session.spec.ts. No fabricated dispatch/query state.

test('malformed rescue blob parse returns null', async () => {
	const mod = await _loadRescueSnapshot();
	assert.equal(mod.parseRescueSnapshot('{not-json'), null);
	assert.equal(mod.parseRescueSnapshot({ schema: 2 }), null);
});
