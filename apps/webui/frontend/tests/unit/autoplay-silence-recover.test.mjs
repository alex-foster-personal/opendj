import assert from 'node:assert/strict';
import { mock, test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const POLL_SETTLE_MS = 900;
const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { AUTO_PLAY_IDLE_MS, resetAutoPlayIdleClock } from '$lib/rb/autoplay-idle';",
	"export { isSilenceRecovering, noteAutoPlaySilenceDropout, resetAutoPlaySilenceRecovery } from '$lib/rb/autoplay-silence-recover';"
].join('\n');

function settle() {
	return new Promise((resolve) => setTimeout(resolve, POLL_SETTLE_MS));
}

function compatibleFeed() {
	return [
		{ stable_id: 'a', key: '8A', bpm: 120, file_exists: true, title: 'A', artist: 'One' },
		{ stable_id: 'b', key: '8A', bpm: 120, file_exists: true, title: 'B', artist: 'Two' },
		{ stable_id: 'c', key: '8A', bpm: 120, file_exists: true, title: 'C', artist: 'Three' },
		{ stable_id: 'd', key: '8A', bpm: 120, file_exists: true, title: 'D', artist: 'Four' }
	];
}

let recover;
let autoPlay;

test.before(async () => {
	recover = await loadTypeScriptModule('src/lib/rb/autoplay-silence-recover.ts');
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
});

test('planAutoPlaySilenceRecovery keeps AutoPlay armed when a next track exists', () => {
	assert.deepEqual(
		recover.planAutoPlaySilenceRecovery({
			enabled: true,
			has_playable_next: true,
			stall_active: false
		}),
		{ action: 'recover' }
	);
	assert.deepEqual(
		recover.planAutoPlaySilenceRecovery({
			enabled: true,
			has_playable_next: false,
			stall_active: true
		}),
		{ action: 'disarm-ok' }
	);
});

test('resolveAutoPlaySourceForTick uses the paused master during recovery', () => {
	recover.noteAutoPlaySilenceDropout({ has_playable_next: true });
	const snaps = [
		{ id: 1, is_master: true, playing: false, stable_id: 'a', position_ms: 1000, duration_ms: 2000 },
		{ id: 2, is_master: false, playing: false, stable_id: null, position_ms: 0, duration_ms: null }
	];
	const resolved = recover.resolveAutoPlaySourceForTick(snaps, autoPlay.pickSourceDeck);
	assert.equal(resolved.source?.stable_id, 'a');
	assert.equal(resolved.remainingOverride, 0);
	recover.resetAutoPlaySilenceRecovery();
});

test('simulateAutoPlayChain still walks three handoffs after a recovery re-arm', () => {
	const playlist = compatibleFeed();
	const bounds = autoPlay.tempoBoundsFromPitchRange(16);
	const chain = autoPlay.simulateAutoPlayChain({
		select_next: autoPlay.pickNextStableId,
		playlist,
		start_stable_id: 'a',
		start_key: '8A',
		start_bpm: 120,
		enforce_play_order: true,
		maximize_reach: false,
		min_tempo_ratio: bounds.min,
		max_tempo_ratio: bounds.max,
		exclude_ids: new Set(),
		played_ids: new Set(['a']),
		max_chain_length: 4
	});
	assert.deepEqual([...chain], ['a', 'b', 'c', 'd']);
});

test('RUNNING it: silence recovery keeps AutoPlay armed past idle threshold', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		mod.setAutoPlayTrackFeed('playlist-a', compatibleFeed());
		await probe.flush();
		mod.deckStates[1].stable_id = 'a';
		mod.deckStates[1].is_master = true;
		mod.deckStates[1].playing = true;
		mod.noteAutoPlaySilenceDropout({ has_playable_next: true });
		mod.deckStates[1].playing = false;
		mod.deckStates[1].audible = false;
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_IDLE_MS + 1);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		assert.equal(mod.isSilenceRecovering(), true);
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		mod?.resetAutoPlaySilenceRecovery();
		probe.restore();
		mock.timers.reset();
	}
});
