/**
 * PLAY-09 / issue #1878: AutoPlay cannot stay armed with every deck stopped.
 * PLAY-12 / issue #3884: 30s silent stall, 31s post-playback disarm, 3min armed-empty grace.
 *
 * [if] AutoPlay is armed and every deck has been stopped for AUTO_PLAY_IDLE_DISARM_MS
 *   with no pending master promotion [then] the pref disarms [⛔️ if aria-pressed
 *   stays true and the hunt records stall:autoplay-idle].
 * [if] a PLAY-08 stall is visible when idle disarm fires [then] the stall banner
 *   stays up [⛔️ if disarming deletes the explanation the room still needs].
 * [if] a deck starts playing again before the threshold [then] AutoPlay stays armed
 *   [⛔️ if a brief pause between tracks disarms the feature].
 * [if] bug #58: every deck stopped because the ENGINE was recovering its audio graph
 *   [then] AutoPlay stays enabled and armed past the idle threshold, and the tag
 *   clears when a deck plays again [⛔️ if a recovery stop switches AutoPlay off].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mock, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const POLL_SETTLE_MS = 900;
const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { readAutoPlayStall, noteAutoPlayExhaustion } from '$lib/rb/autoplay-stall.svelte';",
	"export { clearEngineRecoveryStop, engineRecoveryStopReason, markEngineRecoveryStop } from '$lib/rb/engine-recovery-stop';",
	"export { AUTO_PLAY_ARMED_EMPTY_DISARM_MS, AUTO_PLAY_IDLE_DISARM_MS, AUTO_PLAY_SILENT_STALL_MS, resetAutoPlayIdleClock } from '$lib/rb/autoplay-idle';"
].join('\n');

function settle() {
	return new Promise((resolve) => setTimeout(resolve, POLL_SETTLE_MS));
}

function stopEveryDeck(mod) {
	for (const id of [1, 2, 3, 4]) {
		mod.deckStates[id].playing = false;
	}
}

let autoPlay;

test.before(async () => {
	autoPlay = await loadTypeScriptModule('src/lib/rb/autoplay-idle.ts');
});

test('shouldRaiseAutoPlaySilentStall waits for thirty seconds of idle', () => {
	const { shouldRaiseAutoPlaySilentStall, AUTO_PLAY_SILENT_STALL_MS } = autoPlay;
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
	assert.equal(
		shouldRaiseAutoPlaySilentStall({ ...base, now_ms: 1_000 + AUTO_PLAY_SILENT_STALL_MS }),
		true
	);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, any_playing: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, pending_master: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, silence_recovering: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, stall_active: true }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, source_stable_id: '' }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, enabled: false }), false);
	assert.equal(shouldRaiseAutoPlaySilentStall({ ...base, armed_empty_active: true }), false);
});

test('shouldDisarmAutoPlayIdle waits for the hunt threshold', () => {
	const { shouldDisarmAutoPlayIdle, AUTO_PLAY_IDLE_DISARM_MS } = autoPlay;
	const base = {
		enabled: true,
		any_playing: false,
		pending_master: false,
		idle_since_ms: 1_000,
		now_ms: 1_000 + AUTO_PLAY_IDLE_DISARM_MS - 1,
		armed_empty_active: false
	};
	assert.equal(shouldDisarmAutoPlayIdle(base), false);
	assert.equal(
		shouldDisarmAutoPlayIdle({ ...base, now_ms: 1_000 + AUTO_PLAY_IDLE_DISARM_MS }),
		true
	);
	assert.equal(shouldDisarmAutoPlayIdle({ ...base, any_playing: true }), false);
	assert.equal(shouldDisarmAutoPlayIdle({ ...base, pending_master: true }), false);
	assert.equal(shouldDisarmAutoPlayIdle({ ...base, silence_recovering: true }), false);
	assert.equal(shouldDisarmAutoPlayIdle({ ...base, enabled: false }), false);
});

test('armed-empty mode disarms after three minutes without playback', () => {
	const {
		shouldDisarmAutoPlayIdle,
		shouldRaiseAutoPlaySilentStall,
		AUTO_PLAY_ARMED_EMPTY_DISARM_MS
	} = autoPlay;
	const base = {
		enabled: true,
		any_playing: false,
		pending_master: false,
		stall_active: false,
		idle_since_ms: 1_000,
		now_ms: 1_000 + AUTO_PLAY_ARMED_EMPTY_DISARM_MS - 1,
		source_stable_id: 'src-1',
		armed_empty_active: true
	};
	assert.equal(shouldRaiseAutoPlaySilentStall(base), false);
	assert.equal(shouldDisarmAutoPlayIdle({ ...base, armed_empty_active: true }), false);
	assert.equal(
		shouldDisarmAutoPlayIdle({
			...base,
			now_ms: 1_000 + AUTO_PLAY_ARMED_EMPTY_DISARM_MS,
			armed_empty_active: true
		}),
		true
	);
});

test('planAutoPlayIdleDisarm continues while silence_recovering is true', () => {
	const { planAutoPlayIdleDisarm, AUTO_PLAY_IDLE_DISARM_MS } = autoPlay;
	const plan = planAutoPlayIdleDisarm({
		enabled: true,
		snaps: [{ playing: false }, { playing: false }],
		pending_master: false,
		silence_recovering: true,
		now_ms: AUTO_PLAY_IDLE_DISARM_MS + 5_000,
		stall_active: false
	});
	assert.equal(plan.action, 'continue');
});

test('bug #58: planAutoPlayIdleDisarm never disarms while an engine-recovery stop is tagged', () => {
	const { planAutoPlayIdleDisarm, AUTO_PLAY_IDLE_DISARM_MS, resetAutoPlayIdleClock } = autoPlay;
	const input = {
		enabled: true,
		snaps: [{ playing: false }, { playing: false }],
		pending_master: false,
		stall_active: false
	};
	resetAutoPlayIdleClock();
	planAutoPlayIdleDisarm({ ...input, engine_recovery_stop: 'graph-rebuild-failed', now_ms: 0 });
	const tagged = planAutoPlayIdleDisarm({ ...input, engine_recovery_stop: 'graph-rebuild-failed', now_ms: AUTO_PLAY_IDLE_DISARM_MS * 10 });
	assert.equal(tagged.action, 'continue', 'if a recovery-caused stop disarms AutoPlay then the set cannot resume on its own - broken');
	// Mutation control: the same idle stretch with no tag does disarm.
	resetAutoPlayIdleClock();
	planAutoPlayIdleDisarm({ ...input, engine_recovery_stop: null, now_ms: 0 });
	const untagged = planAutoPlayIdleDisarm({ ...input, engine_recovery_stop: null, now_ms: AUTO_PLAY_IDLE_DISARM_MS + 1 });
	assert.equal(untagged.action, 'disarm');
	resetAutoPlayIdleClock();
});

// REQ: PLAY-09
test('RUNNING it: an engine-recovery stop leaves AutoPlay enabled, and the tag clears when a deck plays (bug #58)', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		await settle();
		stopEveryDeck(mod);
		mod.markEngineRecoveryStop('graph-rebuild-failed');
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_ARMED_EMPTY_DISARM_MS + 1);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true, 'if a recovery-caused stop disarms AutoPlay then the soak set never resumes - broken');
		assert.equal(mod.readAutoPlayStall(), null, 'a recovery stop is not reported as an idle no-deck-playing stall');

		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		await settle();
		assert.equal(mod.engineRecoveryStopReason(), null, 'a deck playing again ends the recovery stop');

		// Control: a later ordinary stop still disarms on the normal clock.
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_IDLE_DISARM_MS + 1);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, false, 'if the tag never clears then AutoPlay can never disarm again - broken');
	} finally {
		if (uninstall !== null) uninstall();
		mod?.clearEngineRecoveryStop();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
});

// REQ: PLAY-09
test('RUNNING it: idle disarm drops the pref after every deck stops', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_IDLE_DISARM_MS + 1);
		await settle();
		assert.equal(
			mod.uiPrefs.auto_play_enabled,
			false,
			'armed AutoPlay with nothing playing must not outlive the idle threshold'
		);
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
});

test('RUNNING it: no-deck-playing stall retires when the same source is audible again', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		mod.deckStates[1].duration_ms = 300_000;
		mod.deckStates[1].position_ms = 167_090;
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_SILENT_STALL_MS + 1);
		await settle();
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-deck-playing');
		assert.equal(mod.readAutoPlayStall()?.source_stable_id, 'src-1');

		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].audible = true;
		mod.deckStates[1].is_master = true;
		await settle();

		assert.equal(
			mod.readAutoPlayStall(),
			null,
			'the same source becoming audible again must retire a no-deck-playing stall'
		);
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
});

test('RUNNING it: thirty seconds idle raises no-deck-playing before disarm', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_SILENT_STALL_MS + 1);
		await settle();
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-deck-playing');
		mock.timers.tick(mod.AUTO_PLAY_IDLE_DISARM_MS);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, false);
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-deck-playing');
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
});

test('RUNNING it: armed-empty grace keeps AutoPlay on for three minutes', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_ARMED_EMPTY_DISARM_MS - 1);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		assert.equal(mod.readAutoPlayStall(), null);
		mock.timers.tick(2);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, false);
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
});

test('RUNNING it: playback before armed-empty grace expires uses normal idle timing', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(60_000);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, true);
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_SILENT_STALL_MS + 1);
		await settle();
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-deck-playing');
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
});

// REQ: PLAY-09
test('RUNNING it: idle disarm keeps a PLAY-08 stall visible', async () => {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		uninstall = mod.installAutoPlay();
		await probe.flush();
		mod.noteAutoPlayExhaustion({
			source_stable_id: 'src-1',
			reason: 'missing-audio',
			blocked: [
				{ stable_id: 'gone-1', key: '8A', bpm: 124, file_exists: false, title: 'Gone', artist: 'Bo' }
			]
		});
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].is_master = true;
		await settle();
		stopEveryDeck(mod);
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_IDLE_DISARM_MS + 1);
		await settle();
		assert.equal(mod.uiPrefs.auto_play_enabled, false);
		assert.notEqual(
			mod.readAutoPlayStall(),
			null,
			'idle disarm must not delete the durable stop explanation'
		);
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
});

test('SHAPE GUARD: idle disarm calls setAutoPlayEnabled from the poll', () => {
	const controller = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);
	const idle = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/autoplay-idle.ts', import.meta.url)),
		'utf8'
	);
	assert.match(controller, /planAutoPlayIdleDisarm\(/);
	assert.match(controller, /applyAutoPlayIdleDisarmAction\(/);
	assert.match(controller, /setAutoPlayEnabled\(false\)/);
	assert.match(controller, /noteAutoPlayArmedEmptyOnEnable\(/);
	assert.match(controller, /noteAutoPlayPlaybackStarted\(/);
	assert.match(idle, /shouldDisarmAutoPlayIdle\(/);
	assert.match(idle, /AUTO_PLAY_ARMED_EMPTY_DISARM_MS/);
});
