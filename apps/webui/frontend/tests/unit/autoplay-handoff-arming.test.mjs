/**
 * PLAY-20: AutoPlay switched off never loads or plays a track afterwards.
 *
 * Tue 6 Oct 2026, build 16 on silver: AutoPlay was switched off by the agent
 * order `opendj autoplay false` at about 14:10:35Z and the mirror read
 * autoplay_enabled false at 14:11:01Z, yet at 14:15:50Z deck 2 loaded a track
 * nobody asked for and started playing it 20 s later, exactly as deck 1 entered
 * its last 16 s. One real gap was found by reading the controller: `_handoff`
 * checked the arming once, before its first await, so an off pressed while a
 * handoff was in flight still loaded and played. Not proven to be the cause of
 * that event.
 *
 * [if] a handoff is in flight when AutoPlay is switched off [then] nothing is
 *   loaded or played after the switch [⛔️ if a deck loads or plays anyway].
 * [if] the off lands after the track is on the deck [then] the track is unloaded
 *   again and the master is untouched [⛔️ if a half-done deck is left behind].
 * [if] AutoPlay is switched off through the agent order bus and the master then
 *   crosses the handoff window [then] no load is dispatched [⛔️ if one is].
 * [if] AutoPlay stays on [then] the same setup does load the next track
 *   [⛔️ if it does not: the two tests above would pass against a dead poll].
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it, test } from 'node:test';

import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

// ----- pure: runAutoPlayHandoff ----------------------------------------------

let handoff;

before(async () => {
	handoff = await loadTypeScriptModule('src/lib/rb/auto-play-handoff.ts');
});

/** A fake deck plus a step log; `disarmAfter` names the step after which AutoPlay goes off. */
function fakeSteps({ occupied = null, disarmAfter = null } = {}) {
	const log = [];
	let armed = true;
	let onDeck = occupied;
	const step = (name, effect) => async () => {
		log.push(name);
		effect?.();
		if (disarmAfter === name) armed = false;
		return name === 'beatSync' ? null : undefined;
	};
	return {
		log,
		onDeck: () => onDeck,
		steps: {
			stillArmed: () => armed,
			followerStableId: () => onDeck,
			unload: step('unload', () => {
				onDeck = null;
			}),
			load: step('load', () => {
				onDeck = 'next-1';
			}),
			beatSync: step('beatSync'),
			play: step('play'),
			notifySyncSkip: () => log.push('syncSkipToast'),
			onPlayDispatched: () => log.push('playDispatched'),
			notifyCleanupFailed: (message) => log.push(`cleanupFailed:${message}`)
		}
	};
}

describe('[PLAY-20] runAutoPlayHandoff re-checks the arming between steps', () => {
	it('armed throughout: unload, load, beat sync, play, in that order', async () => {
		const fake = fakeSteps({ occupied: 'old-1' });
		const outcome = await handoff.runAutoPlayHandoff('next-1', fake.steps);
		assert.equal(outcome, 'handed-off');
		assert.deepEqual(fake.log, ['unload', 'load', 'beatSync', 'play', 'playDispatched']);
		assert.equal(fake.onDeck(), 'next-1');
	});

	it('off before the first step: nothing is touched', async () => {
		const fake = fakeSteps();
		fake.steps.stillArmed = () => false;
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, []);
	});

	it('off while the old track unloads: the next track is never loaded', async () => {
		const fake = fakeSteps({ occupied: 'old-1', disarmAfter: 'unload' });
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, ['unload']);
	});

	it('off while the load is in flight: the track is unloaded again and never played', async () => {
		const fake = fakeSteps({ disarmAfter: 'load' });
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, ['load', 'unload']);
		assert.equal(fake.onDeck(), null, 'no half-done deck is left behind');
	});

	it('off during beat sync: unloaded, never played', async () => {
		const fake = fakeSteps({ disarmAfter: 'beatSync' });
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, ['load', 'beatSync', 'unload']);
		assert.equal(fake.onDeck(), null);
	});

	it('a failed load is still the retryable load phase; a failed play is the commit phase', async () => {
		const failingLoad = fakeSteps();
		failingLoad.steps.load = async () => {
			throw new Error('decode failed');
		};
		await assert.rejects(handoff.runAutoPlayHandoff('next-1', failingLoad.steps), (error) => {
			assert.equal(error.phase, 'load');
			return true;
		});
		const failingPlay = fakeSteps();
		failingPlay.steps.play = async () => {
			throw new Error('play refused');
		};
		await assert.rejects(handoff.runAutoPlayHandoff('next-1', failingPlay.steps), (error) => {
			assert.equal(error.phase, 'commit');
			return true;
		});
	});
});


describe('[PLAY-20] abandon cleanup only touches what this handoff loaded (Sol P1)', () => {
	it('nextId already on the follower, off during beat sync: the deck keeps it', async () => {
		const fake = fakeSteps({ occupied: 'next-1', disarmAfter: 'beatSync' });
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, ['beatSync'], 'no load and, above all, no unload of a track it never loaded');
		assert.equal(fake.onDeck(), 'next-1');
	});

	it('off before the unload of an older track: that track stays on the deck', async () => {
		const fake = fakeSteps({ occupied: 'old-1' });
		fake.steps.stillArmed = () => false;
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, []);
		assert.equal(fake.onDeck(), 'old-1');
	});

	it('older track unloaded on purpose, then off during the load: only the loaded track is undone', async () => {
		const fake = fakeSteps({ occupied: 'old-1', disarmAfter: 'load' });
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, ['unload', 'load', 'unload']);
		assert.equal(fake.onDeck(), null);
	});
});


describe('[PLAY-20] abandon cleanup checks the deck and never blames the candidate (Sol round 2)', () => {
	it('the follower was replaced by someone else after the load: the replacement is left alone', async () => {
		const fake = fakeSteps({ disarmAfter: 'beatSync' });
		const beatSync = fake.steps.beatSync;
		let onDeck = null;
		fake.steps.followerStableId = () => onDeck;
		fake.steps.load = async () => {
			fake.log.push('load');
			onDeck = 'next-1';
		};
		fake.steps.beatSync = async () => {
			onDeck = 'user-pick';
			return beatSync();
		};
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, ['load', 'beatSync'], 'no unload of a track this handoff did not put there');
		assert.equal(onDeck, 'user-pick');
	});

	it('a failed cleanup unload is reported and still abandons, never a commit failure', async () => {
		const fake = fakeSteps({ disarmAfter: 'load' });
		fake.steps.unload = async () => {
			throw new Error('engine busy');
		};
		assert.equal(await handoff.runAutoPlayHandoff('next-1', fake.steps), 'abandoned-disarmed');
		assert.deepEqual(fake.log, ['load', 'cleanupFailed:engine busy']);
	});
});

// ----- live: the real controller, switched off through the agent order bus ----

const POLL_SETTLE_MS = 900;

const ENTRY = [
	"export { installAutoPlay } from '$lib/rb/auto-play.svelte';",
	"export { uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { setAutoPlayTrackFeed } from '$lib/rb/auto-play';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { executeAgentOrder } from '$lib/rb/agent-orders';",
	"export { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';"
].join('\n');

function settle() {
	return new Promise((resolve) => setTimeout(resolve, POLL_SETTLE_MS));
}

function installBrowserShim() {
	const saved = new Map();
	const set = (key, value) => {
		saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
		Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
	};
	set('window', globalThis);
	set('location', { href: 'http://127.0.0.1:0/performance', pathname: '/performance' });
	set('navigator', { userAgent: 'autoplay-handoff-arming-test' });
	set('isSecureContext', true);
	return () => {
		for (const [key, descriptor] of saved) {
			if (descriptor === undefined) delete globalThis[key];
			else Object.defineProperty(globalThis, key, descriptor);
		}
	};
}

/**
 * Deck 1 is the playing master with 60 s left (outside the 16 s window), deck 2
 * is free, and the feed holds one compatible playable track.
 */
async function withArmedSet(run) {
	const probe = installTimerProbe();
	const restoreGlobals = installBrowserShim();
	const realFetch = globalThis.fetch;
	const requests = [];
	globalThis.fetch = async (input) => {
		requests.push(String(typeof input === 'string' || input instanceof URL ? input : input.url));
		// Sol P1: never a fake success. Every request fails loud (503) and is recorded, so
		// the off test proves no handoff request was MADE, not that one quietly succeeded.
		return new Response(JSON.stringify({ detail: 'unit runtime: no engine behind fetch' }), {
			status: 503,
			headers: { 'content-type': 'application/json' }
		});
	};
	const realInfo = console.info;
	const infos = [];
	console.info = (...args) => infos.push(args.join(' '));
	let uninstall = null;
	let uninstallIpc = null;
	try {
		const mod = await loadRuneModule(ENTRY);
		mod.uiPrefs.auto_play_enabled = true;
		uninstall = mod.installAutoPlay();
		uninstallIpc = mod.installPerformanceBrowserIpc();
		mod.setAutoPlayTrackFeed('playlist-a', [
			{ stable_id: 'src-1', key: '8A', bpm: 124, file_exists: true },
			{ stable_id: 'next-1', key: '8A', bpm: 124, file_exists: true }
		]);
		Object.assign(mod.deckStates[1], {
			stable_id: 'src-1',
			playing: true,
			audible: true,
			is_master: true,
			duration_ms: 300_000,
			position_ms: 240_000,
			key: '8A',
			bpm: 124
		});
		await probe.flush();
		await settle();
		const handoffTouched = () =>
			requests.some((url) => url.includes('next-1')) ||
			infos.some((line) => line.includes('next-1')) ||
			mod.deckStates[2].stable_id === 'next-1';
		await run(mod, handoffTouched);
	} finally {
		if (uninstallIpc !== null) uninstallIpc();
		if (uninstall !== null) uninstall();
		console.info = realInfo;
		globalThis.fetch = realFetch;
		restoreGlobals();
		probe.restore();
	}
}

test('[PLAY-20] RUNNING it: off through the order bus, then the window is crossed: no handoff', async () => {
	await withArmedSet(async (mod, handoffTouched) => {
		assert.equal(handoffTouched(), false, 'outside the window nothing is handed off yet');
		const off = await mod.executeAgentOrder({
			kind: 'single',
			payload: { type: 'autoplay', enabled: false, by_user: true }
		});
		assert.deepEqual(off.steps, [{ status: 'succeeded' }]);
		assert.equal(mod.uiPrefs.auto_play_enabled, false);
		mod.deckStates[1].position_ms = 290_000;
		await settle();
		await settle();
		assert.equal(handoffTouched(), false, 'if AutoPlay switched off by order still hands off then broken');
		assert.equal(mod.deckStates[2].stable_id, null);
		assert.equal(mod.deckStates[2].playing, false);
	});
});

test('[PLAY-20] mutation control: left on, the same window crossing does hand off', async () => {
	await withArmedSet(async (mod, handoffTouched) => {
		assert.equal(handoffTouched(), false);
		mod.deckStates[1].position_ms = 290_000;
		await settle();
		await settle();
		assert.equal(
			handoffTouched(),
			true,
			'the armed controller must reach the handoff, or the test above proves nothing'
		);
	});
});

// ----- wiring: the controller hands the handoff its real arming ---------------

/**
 * SHAPE GUARD. The live test above cannot reach the in-flight window: in a
 * unit runtime the follower load fails (no AudioContext), so the handoff never
 * gets past its load step. What it cannot see, this pins: the controller must
 * pass the handoff the SAME arming check `_tick` captured, so wiring
 * `stillArmed: () => true` (or a fresh `_generation` read) is caught here.
 */
test('[PLAY-20] SHAPE GUARD: the controller wires stillArmed to the arming _tick captured', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);
	const start = source.indexOf('async function _handoff(');
	assert.notEqual(start, -1, '_handoff could not be located: this guard asserts nothing');
	const body = source.slice(start, source.indexOf('\n}\n', start));
	assert.match(body, /\bgeneration: number\b/, '_handoff must take the arming generation from _tick');
	assert.match(
		body,
		/runAutoPlayHandoff\(nextId, \{\s*stillArmed: \(\) => _armedAt\(generation\),/,
		'if the controller wires stillArmed to anything but _armedAt(generation) then broken'
	);
	assert.match(
		source,
		/await _handoff\(source, follower, nextId, generation\);/,
		'_tick must pass the generation it armed under, not a fresh read'
	);
});

test('[PLAY-20] SHAPE GUARD: an abandoned handoff gives back its arming claim', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/auto-play.svelte.ts', import.meta.url)),
		'utf8'
	);
	const start = source.indexOf("if (outcome === 'abandoned-disarmed') {");
	assert.notEqual(start, -1, 'the abandon branch could not be located: this guard asserts nothing');
	const branch = source.slice(start, source.indexOf('return;', start));
	for (const reset of ['_triggeredFor = null;', '_claimedIds.delete(nextId);', '_playedIds.delete(nextId);']) {
		assert.ok(
			branch.includes(reset),
			`if an abandoned handoff keeps ${reset.split(/[ .]/)[0]} then a quick re-enable on the same track goes silent - broken`
		);
	}
});
