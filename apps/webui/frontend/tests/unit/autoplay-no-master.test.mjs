/**
 * PLAY-15: AutoPlay names a playing set that has no master.
 *
 * Mon 5 Oct 2026, silver preview 19:19Z to 19:27Z: a reloaded page resumed two
 * decks with no master, AutoPlay (which only arms off the playing master) queued
 * nothing, both tracks played out, and the only signal was the PLAY-08
 * no-deck-playing stall 30 s after the room went quiet.
 *
 * [if] a deck plays with AutoPlay on and no master for 5 s [then] a no-playing-master
 *   stall is raised while the music is still going [⛔️ if AutoPlay stays silent].
 * [if] the master is set and audible [then] the stall retires [⛔️ if it outlives the fix].
 * [if] a master is playing [then] no stall is raised [⛔️ if a healthy set shows a banner].
 */
import assert from 'node:assert/strict';
import { mock, test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const POLL_SETTLE_MS = 900;
const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { readAutoPlayStall } from '$lib/rb/autoplay-stall.svelte';",
	"export { AUTO_PLAY_NO_MASTER_STALL_MS, resetAutoPlayIdleClock } from '$lib/rb/autoplay-idle';"
].join('\n');

function settle() {
	return new Promise((resolve) => setTimeout(resolve, POLL_SETTLE_MS));
}

let idle;
let stall;

test.before(async () => {
	idle = await loadTypeScriptModule('src/lib/rb/autoplay-idle.ts');
	stall = await loadTypeScriptModule('src/lib/rb/autoplay-stall.ts');
});

test('[PLAY-15] shouldRaiseAutoPlayNoMasterStall waits five seconds and needs no master', () => {
	const { shouldRaiseAutoPlayNoMasterStall, AUTO_PLAY_NO_MASTER_STALL_MS } = idle;
	assert.equal(AUTO_PLAY_NO_MASTER_STALL_MS, 5_000);
	const base = {
		enabled: true,
		any_playing: true,
		playing_master: false,
		pending_master: false,
		stall_active: false,
		no_master_since_ms: 1_000,
		now_ms: 1_000 + AUTO_PLAY_NO_MASTER_STALL_MS
	};
	assert.equal(shouldRaiseAutoPlayNoMasterStall(base), true);
	assert.equal(shouldRaiseAutoPlayNoMasterStall({ ...base, now_ms: base.now_ms - 1 }), false);
	assert.equal(shouldRaiseAutoPlayNoMasterStall({ ...base, playing_master: true }), false);
	assert.equal(shouldRaiseAutoPlayNoMasterStall({ ...base, any_playing: false }), false);
	assert.equal(shouldRaiseAutoPlayNoMasterStall({ ...base, pending_master: true }), false);
	assert.equal(shouldRaiseAutoPlayNoMasterStall({ ...base, stall_active: true }), false);
	assert.equal(shouldRaiseAutoPlayNoMasterStall({ ...base, enabled: false }), false);
	assert.equal(shouldRaiseAutoPlayNoMasterStall({ ...base, no_master_since_ms: null }), false);
});

test('[PLAY-15] the no-playing-master stall names its cause and a grep-stable log line', () => {
	const described = stall.describeAutoPlayStall({
		reason: 'no-playing-master',
		source_stable_id: 'src-1',
		blocked: []
	});
	assert.match(described.headline, /no deck is master/);
	assert.match(described.resume, /Press MASTER/);
	assert.equal(
		stall.autoPlayStallDiagnosticMessage('no-playing-master', null),
		'[autoplay] arm failed: no-playing-master, a deck is playing with no master (see autoplay-stall)'
	);
	assert.throws(() => stall.autoPlayExhaustionToast('no-playing-master'), /handoff failure/);
});

async function runController(body) {
	mock.timers.enable({ apis: ['Date'] });
	const probe = installTimerProbe();
	let uninstall = null;
	let mod = null;
	try {
		mod = await loadRuneModule(ENTRY);
		mod.uiPrefs.auto_play_enabled = true;
		uninstall = mod.installAutoPlay();
		await probe.flush();
		await body(mod);
	} finally {
		if (uninstall !== null) uninstall();
		mod?.resetAutoPlayIdleClock();
		probe.restore();
		mock.timers.reset();
	}
}

// REQ: PLAY-15
test('[PLAY-15] RUNNING it: two decks playing with no master raise no-playing-master', async () => {
	await runController(async (mod) => {
		for (const id of [1, 2]) {
			mod.deckStates[id].stable_id = `src-${id}`;
			mod.deckStates[id].playing = true;
			mod.deckStates[id].audible = true;
			mod.deckStates[id].is_master = false;
			mod.deckStates[id].duration_ms = 300_000;
			mod.deckStates[id].position_ms = 60_000;
		}
		await settle();
		assert.equal(mod.readAutoPlayStall(), null, 'no stall before the grace period');
		mock.timers.tick(mod.AUTO_PLAY_NO_MASTER_STALL_MS + 1);
		await settle();
		assert.equal(mod.readAutoPlayStall()?.reason, 'no-playing-master');
		assert.equal(mod.readAutoPlayStall()?.source_stable_id, 'src-1');

		mod.deckStates[1].is_master = true;
		await settle();
		assert.equal(mod.readAutoPlayStall(), null, 'an audible master retires the stall');
	});
});

// REQ: PLAY-15
test('[PLAY-15] RUNNING it: a playing master never raises no-playing-master', async () => {
	await runController(async (mod) => {
		mod.deckStates[1].stable_id = 'src-1';
		mod.deckStates[1].playing = true;
		mod.deckStates[1].audible = true;
		mod.deckStates[1].is_master = true;
		mod.deckStates[1].duration_ms = 300_000;
		mod.deckStates[1].position_ms = 60_000;
		await settle();
		mock.timers.tick(mod.AUTO_PLAY_NO_MASTER_STALL_MS + 1);
		await settle();
		assert.equal(mod.readAutoPlayStall(), null);
	});
});
