/**
 * PLAY-09 / issue #1878, PLAY-12 / issue #3884, revised by PLAY-18 (CORE, Tue 6 Oct 2026):
 * idle NEVER writes the operator's AutoPlay toggle.
 *
 * The 05:21Z and 05:58Z soak stops: the idle path called setAutoPlayEnabled(false),
 * so the mirror read "disabled", identical to the maintainer switching AutoPlay off.
 *
 * Regression lines:
 *   [if] every deck stops past the idle threshold [then] AutoPlay stays enabled, armed=false,
 *     reason no-deck-playing [⛔️ if the toggle drops or the reason reads "disabled"].
 *   [if] a deck plays again [then] AutoPlay re-arms with no user action [⛔️ if it stays disarmed].
 *   [if] a user turns AutoPlay off [then] the mirror reads "disabled" and it stays off, even when a
 *     deck starts playing [⛔️ if a deck playing re-enables it].
 *   [if] a PLAY-08 stall is up when idle passes the threshold [then] the banner stays [⛔️ if lost].
 *   [if] bug #58: the ENGINE stopped the decks [then] no idle clock runs and no stall is raised.
 *   [if] AutoPlay runs out of candidates (no-compatible-track) and the decks then run to their ends
 *     [then] AutoPlay is still enabled and the stall names the exhaustion [⛔️ if it reads "disabled"]
 *     (silver, Tue 6 Oct 2026 09:11Z).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mock, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const POLL_SETTLE_MS = 900;
/** Far past every threshold the old code had (31 s idle, 3 min armed-empty). */
const LONG_IDLE_MS = 10 * 60_000;
const ENTRY = [
	"export { installAutoPlay, readAutoPlayMirrorStatus } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { readAutoPlayStall, noteAutoPlayExhaustion } from '$lib/rb/autoplay-stall.svelte';",
	"export { clearEngineRecoveryStop, engineRecoveryStopReason, markEngineRecoveryStop } from '$lib/rb/engine-recovery-stop';",
	"export { AUTO_PLAY_IDLE_MS, AUTO_PLAY_SILENT_STALL_MS, resetAutoPlayIdleClock } from '$lib/rb/autoplay-idle';",
	"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';"
].join('\n');

function settle() {
	return new Promise((resolve) => setTimeout(resolve, POLL_SETTLE_MS));
}

function stopEveryDeck(mod) {
	for (const id of [1, 2, 3, 4]) {
		mod.deckStates[id].playing = false;
		mod.deckStates[id].audible = false;
	}
}

function playMaster(mod, deck = 1, stableId = 'src-1') {
	Object.assign(mod.deckStates[deck], { stable_id: stableId, playing: true, audible: true, is_master: true, duration_ms: 300_000, position_ms: 10_000 });
}

/** Install the real controller with AutoPlay on, run `body`, always tear down. */
async function withAutoPlay(body) {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		mod.setAutoPlayEnabled(true);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		await body(mod, probe);
	} finally {
		if (uninstall !== null) uninstall();
		mod?.clearEngineRecoveryStop();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
}

let idle;

test.before(async () => {
	idle = await loadTypeScriptModule('src/lib/rb/autoplay-idle.ts');
});

test('shouldRaiseAutoPlaySilentStall waits for thirty seconds of idle', () => {
	const { shouldRaiseAutoPlaySilentStall, AUTO_PLAY_SILENT_STALL_MS } = idle;
	const base = {
		enabled: true,
		any_playing: false,
		pending_master: false,
		stall_active: false,
		idle_since_ms: 1_000,
		now_ms: 1_000 + AUTO_PLAY_SILENT_STALL_MS - 1,
		source_stable_id: 'src-1',
		armed_empty_active: false
	};
	assert.equal(shouldRaiseAutoPlaySilentStall(base), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, now_ms: 1_000 + AUTO_PLAY_SILENT_STALL_MS }), true);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, any_playing: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, pending_master: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, silence_recovering: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, stall_active: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, source_stable_id: '' }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, enabled: false }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, armed_empty_active: true }), false);
});

test('trackAutoPlayIdleClock starts once and holds while idle, and stops for every non-idle reason', () => {
	const { trackAutoPlayIdleClock, resetAutoPlayIdleClock } = idle;
	const input = { enabled: true, snaps: [{ playing: false }], pending_master: false };
	resetAutoPlayIdleClock();
	assert.equal(trackAutoPlayIdleClock({ ...input, now_ms: 5 }), 5);
	assert.equal(trackAutoPlayIdleClock({ ...input, now_ms: LONG_IDLE_MS }), 5, 'the clock keeps its start');
	for (const notIdle of [
		{ snaps: [{ playing: true }] },
		{ pending_master: true },
		{ silence_recovering: true },
		{ engine_recovery_stop: 'graph-rebuild-failed' },
		{ enabled: false }
	]) {
		assert.equal(trackAutoPlayIdleClock({ ...input, ...notIdle, now_ms: 9 }), null, JSON.stringify(notIdle));
	}
	resetAutoPlayIdleClock();
});

test('SHAPE GUARD: the AutoPlay controller never writes the toggle', () => {
	const controller = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);
	assert.doesNotMatch(controller, /setAutoPlayEnabled\(/, 'if the poll can write the toggle again then broken');
	assert.match(controller, /trackAutoPlayIdleClock\(/);
	assert.match(controller, /noteAutoPlayArmedEmptyOnEnable\(/);
	assert.match(controller, /noteAutoPlayPlaybackStarted\(/);
});

// REQ: PLAY-18
test('RUNNING it: idle leaves AutoPlay enabled with reason no-deck-playing', async () => {
	await withAutoPlay(async (mod) => {
		playMaster(mod);
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true, 'if idle drops the toggle then a stall reads like a user stop - broken');
		assert.deepEqual(
			mod.readAutoPlayMirrorStatus(),
			{ autoplay_enabled: true, autoplay_armed: false, autoplay_disarm_reason: 'no-deck-playing' },
			'idle is armed=false with reason no-deck-playing, never "disabled"'
		);
	});
});

// REQ: PLAY-18
test('RUNNING it: a deck playing again re-arms with no user action', async () => {
	await withAutoPlay(async (mod) => {
		playMaster(mod);
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.readAutoPlayMirrorStatus().autoplay_armed, false, 'precondition: idle');
		playMaster(mod);
		await settle();
		assert.deepEqual(mod.readAutoPlayMirrorStatus(), {
			autoplay_enabled: true,
			autoplay_armed: true,
			autoplay_disarm_reason: null
		});
		assert.equal(mod.readAutoPlayStall(), null, 'the no-deck-playing stall retires once a master is audible');
	});
});

// REQ: PLAY-18
test('RUNNING it: a user off reads "disabled" and stays off when a deck starts playing', async () => {
	await withAutoPlay(async (mod, probe) => {
		playMaster(mod);
		await settle();
		mod.setAutoPlayEnabled(false);
		await probe.flush();
		assert.equal(mod.readAutoPlayMirrorStatus().autoplay_disarm_reason, 'disabled');
		// Overshoot control: the re-arm on play must not reach a USER's off.
		stopEveryDeck(mod);
		await settle();
		playMaster(mod, 2, 'src-2');
		await settle();
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, false, 'if a deck playing re-enables a user off then broken');
		assert.deepEqual(mod.readAutoPlayMirrorStatus(), {
			autoplay_enabled: false,
			autoplay_armed: false,
			autoplay_disarm_reason: 'disabled'
		});
	});
});

test('RUNNING it: thirty seconds idle raises no-deck-playing, and it stays up with AutoPlay on', async () => {
	await withAutoPlay(async (mod) => {
		playMaster(mod);
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_SILENT_STALL_MS + 1);
		await settle();
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-deck-playing');
		assert.equal(mod.readAutoPlayStall()?.source_stable_id, 'src-1');
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-deck-playing', 'the stall banner is unchanged');
	});
});

test('RUNNING it: armed-empty (on over silent decks) keeps AutoPlay on and raises no stall', async () => {
	await withAutoPlay(async (mod) => {
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		assert.equal(mod.readAutoPlayStall(), null, 'switching AutoPlay on over silent decks is not a stall');
		assert.equal(mod.readAutoPlayMirrorStatus().autoplay_disarm_reason, 'no-deck-playing');
	});
});

test('RUNNING it: idle keeps an earlier PLAY-08 stall visible', async () => {
	await withAutoPlay(async (mod) => {
		mod.noteAutoPlayExhaustion({
			source_stable_id: 'src-1',
			reason: 'missing-audio',
			blocked: [{ stable_id: 'gone-1', key: '8A', bpm: 124, file_exists: false, title: 'Gone', artist: 'Bo' }]
		});
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		assert.notEqual(mod.readAutoPlayStall(), null, 'idle must not delete the durable stop explanation');
	});
});

// REQ: PLAY-17
test('RUNNING it: an engine-recovery stop raises no idle stall, and the tag clears when a deck plays (bug #58)', async () => {
	await withAutoPlay(async (mod) => {
		playMaster(mod);
		await settle();
		stopEveryDeck(mod);
		mod.markEngineRecoveryStop('graph-rebuild-failed');
		await settle();
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		assert.equal(mod.readAutoPlayStall(), null, 'a recovery stop is not reported as an idle no-deck-playing stall');
		playMaster(mod);
		await settle();
		assert.equal(mod.engineRecoveryStopReason(), null, 'a deck playing again ends the recovery stop');
	});
});

// REQ: PLAY-18
test('RUNNING it: exhaustion (no-compatible-track) then the decks running out leaves AutoPlay enabled (silver 09:11Z)', async () => {
	await withAutoPlay(async (mod) => {
		// The only feed row is the track already playing, so the real picker finds
		// nothing and the controller raises its own exhaustion stall.
		mod.setAutoPlayTrackFeed('exhaustion-fixture', [
			{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true, title: 'Src', artist: 'A' }
		]);
		Object.assign(mod.deckStates[1], {
			stable_id: 'src-1', playing: true, audible: true, is_master: true, duration_ms: 300_000, position_ms: 295_000
		});
		await settle();
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-compatible-track', 'control: the controller itself raised the exhaustion stall');
		// Both decks at their exact ends, as in the silver snapshot.
		mod.deckStates[1].position_ms = 300_000;
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(LONG_IDLE_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true, 'if exhaustion plus idle writes the toggle off then broken');
		const status = mod.readAutoPlayMirrorStatus();
		assert.equal(status.autoplay_enabled, true);
		assert.equal(status.autoplay_armed, false);
		assert.notEqual(status.autoplay_disarm_reason, 'disabled', 'an exhaustion stop must never read like a user stop');
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-compatible-track', 'the exhaustion explanation survives the idle period');
	});
});
