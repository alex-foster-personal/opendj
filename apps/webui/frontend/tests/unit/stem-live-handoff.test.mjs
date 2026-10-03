// requirement STEM-47: stems that finish loading while a deck is PLAYING take
// over the deck without stopping it, so the stem buttons work on a track the
// DJ has already started.
//
// Before this, a finished stem bundle was held until the deck next stopped. A
// DJ who loaded a track and pressed play before the stems landed (about 2 s
// for a local bundle, longer for a cloud one) kept dead stem buttons for the
// whole play.
//
// [if] the deck is playing and idle [then] the stems are scheduled to start at
//   one future instant at the projected position, the mix is stopped at that
//   SAME instant, and only then is the deck committed to the stems
// [if] a transport command lands while the stems' schedule is being
//   acknowledged [then] the handoff is canceled and nothing is committed
// [if] the acknowledgement arrives too late to start cleanly [then] canceled
// [if] scheduling the stems fails [then] the error propagates and the playing
//   mix is never touched
// [if] the deck is not idle [then] nothing is scheduled at all
// [if] the mix refuses its stop [then] it is retired at once (never doubled)
// [if] the deck has stopped [then] the ordinary stopped adoption is used
// [if] the deck stays busy for every attempt [then] the outcome is 'deferred'
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/stem-live-handoff.ts');
});

const SEGMENT = { positionSec: 42.5, tempoRatio: 1.02, masterTempoEnabled: true, keyShiftSemitones: 0, loop: null };

/** A playing, idle deck on a controllable clock. Every dep call is logged in order. */
function deck(overrides = {}) {
	const log = [];
	const state = {
		now: 100,
		snap: {
			active: true,
			revision: 7,
			intents: 0,
			pendingCount: 0,
			transportPending: false,
			launchArmed: false,
			latencyMatches: true
		},
		replaceable: false,
		stale: false
	};
	const deps = {
		now: () => state.now,
		leadSec: 0.05,
		snapshot: () => ({ ...state.snap }),
		segmentAt: (when) => {
			log.push(['segmentAt', when]);
			return { ...SEGMENT };
		},
		scheduleIncoming: async (when, segment) => {
			log.push(['scheduleIncoming', when, segment.positionSec]);
		},
		cancelIncoming: async (when) => {
			log.push(['cancelIncoming', when]);
		},
		connectIncoming: () => {
			log.push(['connectIncoming']);
		},
		stopOutgoing: async (when) => {
			log.push(['stopOutgoing', when]);
		},
		retireRefusedOutgoing: (when, error) => {
			log.push(['retireRefusedOutgoing', when, error.message]);
		},
		commit: (when, segment) => {
			log.push(['commit', when, segment.positionSec]);
		},
		holdCommandsUntil: (when) => {
			log.push(['holdCommandsUntil', when]);
		},
		stale: () => state.stale,
		replaceable: () => state.replaceable,
		adoptStopped: () => {
			log.push(['adoptStopped']);
		},
		sleep: async (ms) => {
			log.push(['sleep', ms]);
		},
		...overrides
	};
	return { deps, log, state };
}

const names = (log) => log.map((entry) => entry[0]);

test('a playing idle deck hands off at one shared instant, stems first', async () => {
	const { deps, log } = deck();
	const outcome = await mod.handOffStemsLive(deps, 0.15);
	assert.equal(outcome, 'handed_off');
	assert.deepEqual(names(log), ['segmentAt', 'scheduleIncoming', 'connectIncoming', 'stopOutgoing', 'commit', 'holdCommandsUntil']);
	const when = log[0][1];
	assert.ok(when >= 100 + 0.05 + 0.15, `handoff instant ${when} is inside the processor lead`);
	// The whole point: one instant for both sides. A different stop time is a
	// gap (later start) or a doubled deck (later stop).
	assert.equal(log[1][1], when, 'stems are not scheduled at the handoff instant');
	assert.equal(log[3][1], when, 'the mix does not stop at the instant the stems start');
	assert.equal(log[4][1], when);
	assert.equal(log[1][2], SEGMENT.positionSec, 'stems must start at the projected position');
});

test('a transport command during the acknowledgement cancels the handoff', async () => {
	const { deps, log, state } = deck();
	deps.scheduleIncoming = async (when) => {
		log.push(['scheduleIncoming', when]);
		state.snap.revision += 1; // a pause/seek/tempo change was scheduled on the mix
	};
	const outcome = await mod.handOffStemsLive(deps, 0.15);
	assert.equal(outcome, 'moved');
	assert.deepEqual(names(log), ['segmentAt', 'scheduleIncoming', 'cancelIncoming']);
	// The cancel must name the instant the start was scheduled at: the worklet
	// replaces a scheduled change only with one at the same or an earlier time.
	assert.equal(log[2][1], log[1][1], 'the cancel does not target the scheduled start');
});

test('a command still in flight after the acknowledgement cancels the handoff', async () => {
	const { deps, log, state } = deck();
	deps.scheduleIncoming = async () => {
		state.snap.intents = 1;
	};
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'moved');
	assert.ok(!names(log).includes('commit'));
	assert.ok(!names(log).includes('stopOutgoing'));
});

test('an acknowledgement that lands too late to start cleanly is canceled', async () => {
	const { deps, log, state } = deck();
	deps.scheduleIncoming = async () => {
		state.now += 0.19; // only 10ms left before the instant; the lead is 50ms
	};
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'moved');
	assert.deepEqual(names(log), ['segmentAt', 'cancelIncoming']);
});

test('a deck that goes stale during the acknowledgement lands nothing', async () => {
	// Unloaded, or stems switched off by a mode, while the stems' schedule was
	// in flight: the transport looks untouched, so only the stale read stops it.
	const { deps, log, state } = deck();
	deps.scheduleIncoming = async () => {
		state.stale = true;
	};
	assert.equal(await mod.landStemUpgrade(deps), 'stale');
	assert.deepEqual(names(log), ['segmentAt', 'cancelIncoming', 'sleep']);
	assert.ok(!names(log).includes('commit') && !names(log).includes('stopOutgoing'));
});

test('a failed stem schedule never touches the playing mix', async () => {
	const { deps, log } = deck();
	deps.scheduleIncoming = async () => {
		throw new Error('worklet refused');
	};
	await assert.rejects(mod.handOffStemsLive(deps, 0.15), /worklet refused/);
	assert.deepEqual(names(log), ['segmentAt']);
});

for (const [label, patch] of [
	['paused', { active: false }],
	['a queued command', { intents: 1 }],
	['a pending schedule', { pendingCount: 1 }],
	['a pending transport', { transportPending: true }],
	['an armed quantized launch', { launchArmed: true }],
	['a different processor latency', { latencyMatches: false }]
]) {
	test(`a deck with ${label} is not handed off live`, async () => {
		const { deps, log, state } = deck();
		Object.assign(state.snap, patch);
		assert.equal(await mod.handOffStemsLive(deps, 0.15), 'busy');
		assert.deepEqual(log, [], 'a busy deck must have nothing scheduled on either processor');
	});
}

test('a mix that refuses its stop is retired at the handoff instant with its error, never left doubling the stems', async () => {
	const { deps, log } = deck();
	deps.stopOutgoing = async () => {
		throw new Error('stop refused');
	};
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'handed_off');
	await new Promise((resolve) => setImmediate(resolve));
	const when = log.find((entry) => entry[0] === 'commit')[1];
	assert.deepEqual(log.at(-1), ['retireRefusedOutgoing', when, 'stop refused']);
});

test('a stopped deck takes the ordinary stopped adoption', async () => {
	const { deps, log, state } = deck();
	state.replaceable = true;
	assert.equal(await mod.landStemUpgrade(deps), 'adopted');
	assert.deepEqual(names(log), ['adoptStopped']);
});

test('a busy deck is retried, then handed off once it is idle', async () => {
	const { deps, log, state } = deck();
	state.snap.intents = 1;
	let sleeps = 0;
	deps.sleep = async (ms) => {
		log.push(['sleep', ms]);
		sleeps += 1;
		if (sleeps === 2) state.snap.intents = 0;
	};
	assert.equal(await mod.landStemUpgrade(deps), 'handed_off');
	assert.equal(sleeps, 2);
	assert.deepEqual(names(log).slice(-2), ['commit', 'holdCommandsUntil']);
});

test('a deck that goes stale mid-retry lands nothing', async () => {
	const { deps, log, state } = deck();
	state.snap.intents = 1;
	deps.sleep = async () => {
		state.stale = true;
	};
	assert.equal(await mod.landStemUpgrade(deps), 'stale');
	assert.ok(!names(log).includes('commit'));
	assert.ok(!names(log).includes('adoptStopped'));
});

test('a deck that stays busy for every attempt is reported deferred, not dropped', async () => {
	const { deps, log, state } = deck();
	state.snap.transportPending = true;
	assert.equal(await mod.landStemUpgrade(deps), 'deferred');
	assert.equal(
		names(log).filter((name) => name === 'sleep').length,
		mod.STEM_HANDOFF_MARGINS_SEC.length - 1,
		'one wait between attempts, none after the last'
	);
	assert.ok(!names(log).includes('commit'));
});

test('each retry gives the acknowledgement more room', () => {
	const margins = mod.STEM_HANDOFF_MARGINS_SEC;
	assert.ok(margins.length >= 3);
	for (let i = 1; i < margins.length; i += 1) {
		assert.ok(margins[i] > margins[i - 1], 'margins must grow, or a slow ack fails forever');
	}
});

// ------------------------------------------------ the deck binding (STEM-47)
//
// landStemsOnDeck is what the engine calls. These drive it with a fake deck
// runtime and fake processors on a controllable clock.
//
// [if] the stems are connected to the deck before their schedule is acknowledged in time [then ⛔️] (a late ack would leave them audible beside the mix)
// [if] the mix is stopped at another instant than the stems start [then ⛔️]
// [if] the mix is retired before its stop instant has passed [then ⛔️]
// [if] the stems' latency differs from the deck's [then] nothing is scheduled
// [if] the landing ends stale [then] the incoming processor is retired
// [if] the landing ends deferred [then] the bundle is handed back, not retired
// [if] the deck graph is gone [then] the landing rejects, mix untouched

function landing(overrides = {}) {
	const log = [];
	const timers = [];
	const clock = { currentTime: 100, sampleRate: 48000 };
	const processor = (name, latency) => ({
		name,
		connect: (node) => log.push([`${name}.connect`, node]),
		schedule: async (when, change) => {
			log.push([`${name}.schedule`, when, change]);
		},
		stop: async (when) => {
			log.push([`${name}.stop`, when]);
		},
		latencySec: async () => latency
	});
	const mix = processor('mix', 0.1);
	const stems = processor('stems', 0.1);
	const runtime = {
		processor: mix,
		nodes: { analyser: 'analyser' },
		latencySec: 0.1,
		durationSec: 300,
		controlActive: true,
		nextScheduleRevision: 3,
		scheduleIntentCount: 0,
		pending: [],
		scheduleTail: Promise.resolve()
	};
	const port = {
		runtime,
		incoming: stems,
		clock,
		leadSec: 0.05,
		serialized: (run) => {
			log.push(['serialized']);
			return run();
		},
		commitDue: () => {},
		rampPending: () => false,
		launchArmed: () => false,
		controlSegmentAt: (when) => ({
			startContextTime: 90,
			startPositionSec: 10,
			tempoRatio: 1.25,
			active: true,
			loop: null
		}),
		startChange: (segment) => ({ start: segment.positionSec }),
		retire: (target) => log.push(['retire', target.name]),
		commit: (when) => {
			log.push(['commit', when]);
			runtime.processor = stems;
		},
		defer: () => log.push(['defer']),
		stale: () => false,
		replaceable: () => false,
		adoptStopped: () => log.push(['adoptStopped']),
		setTimer: (run, ms) => {
			timers.push({ run, ms });
			if (overrides.autoTimers !== false) queueMicrotask(run);
		},
		...overrides.port
	};
	return { port, log, timers, runtime, clock, mix, stems };
}

test('a playing deck: stems are scheduled, connected once acknowledged, then start as the mix stops', async () => {
	const { port, log } = landing();
	assert.equal(await mod.landStemsOnDeck(port), 'handed_off');
	const order = names(log);
	assert.equal(order[0], 'serialized', 'the landing ran outside the deck swap queue');
	const connect = order.indexOf('stems.connect');
	const schedule = order.indexOf('stems.schedule');
	const stop = order.indexOf('mix.stop');
	const commit = order.indexOf('commit');
	assert.ok(schedule >= 0 && schedule < connect, 'stems were connected before their schedule was acknowledged');
	assert.ok(connect < stop && stop < commit, `wrong order: ${order.join(', ')}`);
	assert.equal(log[connect][1], 'analyser', 'stems are not wired into the deck channel');
	const when = log[schedule][1];
	assert.equal(log[stop][1], when, 'the mix does not stop at the instant the stems start');
	assert.equal(log[commit][1], when);
	// The segment began at ctx 90 on position 10 and runs at tempo 1.25.
	assert.ok(Math.abs(log[schedule][2].start - (10 + (when - 90) * 1.25)) < 1e-9, 'stems start at the wrong position');
});

test('the stopped mix is retired only after its stop instant has passed', async () => {
	const { port, log, timers } = landing({ autoTimers: false });
	const done = mod.landStemsOnDeck(port);
	assert.equal(await done, 'handed_off');
	assert.ok(!names(log).includes('retire'), 'the mix was retired while it could still be the audible tail');
	const when = log.find((entry) => entry[0] === 'mix.stop')[1];
	assert.equal(timers.length, 3);
	// The stop was acknowledged, so the timer at the handoff instant does nothing.
	timers[0].run();
	assert.ok(!names(log).includes('retire'), 'an acknowledged stop was retired at the instant it renders');
	const expectedMs = (when - 100 + mod.STEM_HANDOFF_RETIRE_AFTER_SEC) * 1000;
	assert.ok(Math.abs(timers[1].ms - expectedMs) < 1e-6, `retire timer ${timers[1].ms}ms, expected ${expectedMs}ms`);
	timers[1].run();
	assert.deepEqual(log.at(-1), ['retire', 'mix']);
});

test('a command sent before the handoff instant waits for it, so it never reaches the stems beside the mix', async () => {
	const { port, log, timers } = landing({ autoTimers: false });
	assert.equal(await mod.landStemsOnDeck(port), 'handed_off');
	const when = log.find((entry) => entry[0] === 'commit')[1];
	let commandRan = false;
	const command = port.runtime.scheduleTail.then(() => (commandRan = true));
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(commandRan, false, 'a transport command reached the stems while the mix was still audible');
	const hold = timers.find((timer) => Math.abs(timer.ms - (when - 100) * 1000) < 1e-6 && timer !== timers[0]);
	assert.ok(hold, 'no hold was armed at the handoff instant');
	hold.run();
	await command;
	assert.equal(commandRan, true);
});

test('a mix whose stop is still unacknowledged when the stems start is retired then', async () => {
	const { port, log, timers } = landing({ autoTimers: false });
	port.runtime.processor.stop = () => new Promise(() => {}); // the ack never comes back in time
	assert.equal(await mod.landStemsOnDeck(port), 'handed_off');
	const when = log.find((entry) => entry[0] === 'commit')[1];
	assert.ok(Math.abs(timers[0].ms - (when - 100) * 1000) < 1e-6, `first retire check at ${timers[0].ms}ms, expected the handoff instant`);
	timers[0].run();
	assert.deepEqual(log.filter((entry) => entry[0] === 'retire'), [['retire', 'mix']], 'the unstopped mix stayed under the stems');
	timers[1].run();
	assert.equal(log.filter((entry) => entry[0] === 'retire').length, 1, 'the mix was retired twice');
});

test('a mix that refuses its stop keeps playing until the stems start, then is retired once', async () => {
	const { port, log, timers } = landing({ autoTimers: false });
	port.runtime.processor.stop = async () => {
		throw new Error('stop timed out');
	};
	const errors = [];
	const consoleError = console.error;
	console.error = (...args) => errors.push(args);
	try {
		assert.equal(await mod.landStemsOnDeck(port), 'handed_off');
		await new Promise((resolve) => setImmediate(resolve));
	} finally {
		console.error = consoleError;
	}
	assert.ok(!names(log).includes('retire'), 'the mix was retired before the stems were audible: the deck goes silent');
	assert.ok(errors.some((args) => args.some((arg) => arg instanceof Error && arg.message === 'stop timed out')), 'the stop error was swallowed');
	const when = log.find((entry) => entry[0] === 'commit')[1];
	assert.equal(timers.length, 4);
	assert.ok(Math.abs(timers[3].ms - (when - 100) * 1000) < 1e-6, `refused-stop retire at ${timers[3].ms}ms, expected the handoff instant`);
	timers[3].run();
	timers[0].run();
	timers[1].run();
	assert.deepEqual(log.filter((entry) => entry[0] === 'retire'), [['retire', 'mix']], 'the mix was retired twice');
});

test('stems with a different latency than the deck are never scheduled live', async () => {
	const { port, log } = landing();
	port.incoming.latencySec = async () => 0.2;
	assert.equal(await mod.landStemsOnDeck(port), 'deferred');
	assert.ok(!names(log).includes('stems.schedule') && !names(log).includes('mix.stop'));
	assert.equal(names(log).at(-1), 'defer', 'a deferred bundle was not handed back to the deck');
	assert.ok(!names(log).includes('retire'), 'a deferred bundle was retired: the stems are lost');
});

test('a landing that ends stale retires the incoming processor', async () => {
	const { port, log } = landing({ port: { stale: () => true } });
	assert.equal(await mod.landStemsOnDeck(port), 'stale');
	assert.deepEqual(log.at(-1), ['retire', 'stems']);
	assert.ok(!names(log).includes('defer') && !names(log).includes('commit'));
});

test('a stopped deck is adopted through the port, with nothing scheduled', async () => {
	const { port, log } = landing({ port: { replaceable: () => true } });
	assert.equal(await mod.landStemsOnDeck(port), 'adopted');
	assert.deepEqual(names(log), ['serialized', 'adoptStopped']);
});

test('a deck with no audio graph rejects and leaves the mix alone', async () => {
	const { port, log, runtime } = landing();
	runtime.nodes = null;
	await assert.rejects(mod.landStemsOnDeck(port), /audio graph is missing/);
	assert.ok(!names(log).includes('mix.stop') && !names(log).includes('commit'));
});

test('the binding reads the deck live: a command queued mid-landing blocks the handoff', async () => {
	const { port, log, runtime } = landing();
	port.incoming.schedule = async (when) => {
		log.push(['stems.schedule', when]);
		runtime.nextScheduleRevision += 1;
		runtime.scheduleIntentCount = 1;
	};
	assert.equal(await mod.landStemsOnDeck(port), 'deferred');
	assert.ok(names(log).includes('stems.stop'), 'the scheduled stems start was not canceled');
	assert.ok(!names(log).includes('mix.stop') && !names(log).includes('commit'));
	assert.ok(!names(log).includes('stems.connect'), 'a canceled schedule was connected to the deck');
});

test('an acknowledgement that lands after the handoff instant never connects the stems', async () => {
	// [if] the schedule is acknowledged after its instant [then] the stems are never connected to the deck, the mix is untouched, [else stop]
	const { port, log, clock } = landing();
	let late = 0;
	port.incoming.schedule = async (when) => {
		log.push(['stems.schedule', when]);
		late += 1;
		clock.currentTime = when + 0.01; // the ack arrives after the instant
	};
	assert.equal(await mod.landStemsOnDeck(port), 'deferred');
	assert.equal(late, mod.STEM_HANDOFF_MARGINS_SEC.length, 'every attempt should have been late');
	assert.ok(!names(log).includes('stems.connect'), 'a late schedule was connected and could play beside the mix');
	assert.ok(!names(log).includes('mix.stop') && !names(log).includes('commit'));
});

// ------------------------------------------- LOAD NOW on a held bundle (STEM-47)
//
// [if] landing a held bundle rejects [then] the bundle is retired, the deck reads a retryable error, and the caller is told, [else stop]
// [if] the deck already points at the held processor when the landing rejects [then] it is not retired

function heldPort(overrides = {}) {
	const log = [];
	return {
		log,
		port: {
			land: async () => {
				throw new Error('schedule rejected');
			},
			adopted: () => false,
			retire: () => log.push(['retire']),
			stale: () => false,
			fail: (message) => log.push(['fail', message]),
			...overrides
		}
	};
}

test('a held landing that rejects retires the bundle and settles the deck to a retryable error', async () => {
	const { port, log } = heldPort();
	await assert.rejects(mod.landHeldStemsOrSettle(port), /schedule rejected/);
	assert.deepEqual(log, [['retire'], ['fail', 'schedule rejected']]);
});

test('a held landing that rejects after the deck adopted the stems keeps them', async () => {
	const { port, log } = heldPort({ adopted: () => true });
	await assert.rejects(mod.landHeldStemsOrSettle(port));
	assert.deepEqual(log, [['fail', 'schedule rejected']]);
});

test('a held landing that rejects on a stale load retires it and leaves the new load alone', async () => {
	const { port, log } = heldPort({ stale: () => true });
	await assert.rejects(mod.landHeldStemsOrSettle(port));
	assert.deepEqual(log, [['retire']]);
});

test('control: a held landing that succeeds touches nothing', async () => {
	const { port, log } = heldPort({ land: async () => 'handed_off' });
	await mod.landHeldStemsOrSettle(port);
	assert.deepEqual(log, []);
});
