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
// [if] the mix refuses its stop (which poisons it) [then] the stems take the
//   deck, the mix is cut at the handoff instant, and the refusal is reported
// [if] the stems' cancel fails during a rollback [then] the mix is still put
//   back, and the cancel's error propagates
// [if] the mix's stop is still unacknowledged at the handoff instant [then] it
//   is cut there on its own timer, and the stems take the deck
// [if] the stop fails while the upgrade went stale [then] the deck is failed,
//   never left pointing at the dead mix
// [if] the deck commits before the mix acknowledged its stop [then ⛔️]
// [if] the mix's stop is acknowledged too late [then] both are rolled back at
//   the first instant still ahead, and nothing is committed
// [if] the deck has stopped [then] the ordinary stopped adoption is used
// [if] the deck stays busy for every attempt [then] the outcome is 'deferred'
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/stem-live-handoff.ts');
});

const SEGMENT = { active: true, positionSec: 42.5, tempoRatio: 1.02, masterTempoEnabled: true, keyShiftSemitones: 0, loop: null };

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
		stale: false,
		cut: false,
		cutChecks: []
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
		stopOutgoing: async (when) => {
			log.push(['stopOutgoing', when]);
		},
		restoreOutgoing: async (at, segment) => {
			log.push(['restoreOutgoing', at, segment.positionSec]);
		},
		retireOutgoingAfter: (when, tailSec) => {
			log.push(['retireOutgoingAfter', when, tailSec]);
		},
		reportOutgoingFailure: (error) => {
			log.push(['reportOutgoingFailure', error.message]);
		},
		cutOutgoingAt: (when, stillPending) => {
			state.cutChecks.push({ when, stillPending });
			return () => state.cut;
		},
		failOutgoing: (error) => {
			log.push(['failOutgoing', error.message]);
		},
		commit: (when, segment) => {
			log.push(['commit', when, segment.positionSec]);
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
	assert.deepEqual(names(log), ['segmentAt', 'scheduleIncoming', 'stopOutgoing', 'commit', 'retireOutgoingAfter']);
	const when = log[0][1];
	assert.ok(when >= 100 + 0.05 + 0.15, `handoff instant ${when} is inside the processor lead`);
	// The whole point: one instant for both sides. A different stop time is a
	// gap (later start) or a doubled deck (later stop).
	assert.equal(log[1][1], when, 'stems are not scheduled at the handoff instant');
	assert.equal(log[2][1], when, 'the mix does not stop at the instant the stems start');
	assert.equal(log[3][1], when);
	assert.equal(log[4][1], when, 'the mix must be retired after the handoff instant, not another');
	assert.equal(log[4][2], mod.STEM_HANDOFF_RETIRE_AFTER_SEC, 'an acknowledged stop keeps its audible tail');
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

test('a mix that refuses its stop is cut at the handoff instant and the stems take the deck', async () => {
	// A refused or timed-out command poisons the mix: it cannot be put back, so
	// staying on it would leave the deck silent while it reads playing.
	const { deps, log } = deck();
	deps.stopOutgoing = async (when) => {
		log.push(['stopOutgoing', when]);
		throw new Error('stop refused');
	};
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'handed_off');
	assert.deepEqual(names(log), ['segmentAt', 'scheduleIncoming', 'stopOutgoing', 'commit', 'retireOutgoingAfter', 'reportOutgoingFailure']);
	const when = log[1][1];
	assert.deepEqual(log[4], ['retireOutgoingAfter', when, 0], 'the dead mix must be cut at the handoff instant, with no tail');
	assert.deepEqual(log[5], ['reportOutgoingFailure', 'stop refused'], 'the refusal was swallowed');
	assert.ok(!names(log).includes('cancelIncoming') && !names(log).includes('restoreOutgoing'));
});

test('a stop still unacknowledged at the handoff instant is cut there, never doubled', async () => {
	const { deps, log, state } = deck();
	let ack;
	deps.stopOutgoing = (when) => {
		log.push(['stopOutgoing', when]);
		return new Promise((resolve) => {
			ack = resolve;
		});
	};
	const outcome = mod.handOffStemsLive(deps, 0.15);
	await new Promise((resolve) => setImmediate(resolve));
	const when = log[1][1];
	assert.equal(state.cutChecks.length, 1, 'no independent cut was armed for the mix');
	assert.equal(state.cutChecks[0].when, when, 'the cut is not at the handoff instant');
	assert.equal(state.cutChecks[0].stillPending(), true, 'the cut must fire while the stop is unsettled');
	state.cut = true; // the timer fired at `when`
	ack(); // the ack lands after the cut
	assert.equal(await outcome, 'handed_off');
	assert.ok(!names(log).includes('restoreOutgoing'), 'a cut mix was "restored"');
	assert.ok(!names(log).includes('retireOutgoingAfter'), 'the cut mix was retired twice');
	assert.deepEqual(names(log).slice(-2), ['commit', 'reportOutgoingFailure']);
});

test('control: a stop acknowledged in time disarms the cut', async () => {
	const { deps, state } = deck();
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'handed_off');
	assert.equal(state.cutChecks[0].stillPending(), false, 'the cut would fire after an acknowledged stop');
});

test('a failed stop on an upgrade that went stale fails the deck instead of leaving the dead mix', async () => {
	const { deps, log, state } = deck();
	deps.stopOutgoing = async (when) => {
		log.push(['stopOutgoing', when]);
		state.stale = true;
		throw new Error('stop timed out');
	};
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'moved');
	assert.deepEqual(log.at(-1), ['failOutgoing', 'stop timed out']);
	assert.ok(!names(log).includes('commit'));
});

test('a rollback whose stems cancel fails still puts the mix back', async () => {
	const { deps, log, state } = deck();
	deps.stopOutgoing = async (when) => {
		log.push(['stopOutgoing', when]);
		state.snap.revision += 1;
	};
	deps.cancelIncoming = async () => {
		throw new Error('cancel refused');
	};
	await assert.rejects(mod.handOffStemsLive(deps, 0.15), /cancel refused/);
	assert.equal(names(log).at(-1), 'restoreOutgoing', 'the mix was left stopped after the cancel failed');
	assert.ok(!names(log).includes('commit'));
});

test('the deck is not committed until the mix acknowledges its stop', async () => {
	const { deps, log } = deck();
	let ack;
	deps.stopOutgoing = (when) => {
		log.push(['stopOutgoing', when]);
		return new Promise((resolve) => {
			ack = resolve;
		});
	};
	const outcome = mod.handOffStemsLive(deps, 0.15);
	await new Promise((resolve) => setImmediate(resolve));
	assert.ok(!names(log).includes('commit'), 'committed to the stems before the mix acknowledged its stop');
	assert.ok(!names(log).includes('retireOutgoingAfter'), 'retirement armed before the stop was acknowledged');
	ack();
	assert.equal(await outcome, 'handed_off');
	assert.deepEqual(names(log).slice(-2), ['commit', 'retireOutgoingAfter']);
});

test('a mix stop acknowledged too late rolls back at the first instant still ahead', async () => {
	const { deps, log, state } = deck();
	deps.stopOutgoing = async (when) => {
		log.push(['stopOutgoing', when]);
		state.now += 5; // the command timeout: the handoff instant is long gone
	};
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'moved');
	assert.ok(!names(log).includes('commit'));
	const cancel = log.find((entry) => entry[0] === 'cancelIncoming');
	const restore = log.find((entry) => entry[0] === 'restoreOutgoing');
	assert.ok(cancel[1] >= state.now + deps.leadSec, `rollback at ${cancel[1]} is behind the clock`);
	assert.equal(restore[1], cancel[1], 'the mix and the stems were rolled back at different instants');
});

test('a command that lands while the mix stop is acknowledged rolls both back', async () => {
	// Control for the overshoot: the second await must be checked too.
	const { deps, log, state } = deck();
	deps.stopOutgoing = async (when) => {
		log.push(['stopOutgoing', when]);
		state.snap.revision += 1;
	};
	assert.equal(await mod.handOffStemsLive(deps, 0.15), 'moved');
	assert.deepEqual(names(log).slice(-3), ['cancelIncoming', 'segmentAt', 'restoreOutgoing']);
	assert.ok(!names(log).includes('commit'));
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
	assert.deepEqual(names(log).slice(-2), ['commit', 'retireOutgoingAfter']);
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
// [if] the stems are scheduled before they are connected [then ⛔️]
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
		pending: []
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
		outgoingFailed: () => {},
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
			// After pending acks settle, as a real timer would for any delay > 0.
			if (overrides.autoTimers !== false) setImmediate(run);
		},
		...overrides.port
	};
	return { port, log, timers, runtime, clock, mix, stems };
}

test('a playing deck: stems connect, then start at the instant the mix stops', async () => {
	const { port, log } = landing();
	assert.equal(await mod.landStemsOnDeck(port), 'handed_off');
	const order = names(log);
	assert.equal(order[0], 'serialized', 'the landing ran outside the deck swap queue');
	const connect = order.indexOf('stems.connect');
	const schedule = order.indexOf('stems.schedule');
	const stop = order.indexOf('mix.stop');
	const commit = order.indexOf('commit');
	assert.ok(connect >= 0 && connect < schedule, 'stems were scheduled before they were connected');
	assert.ok(schedule < stop && stop < commit, `wrong order: ${order.join(', ')}`);
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
	assert.equal(timers.length, 2, 'expected the cut timer and the retire timer');
	assert.ok(Math.abs(timers[0].ms - (when - 100) * 1000) < 1e-6, 'the cut timer is not at the handoff instant');
	timers[0].run();
	assert.ok(!names(log).includes('retire'), 'an acknowledged stop was cut at the handoff instant, losing its tail');
	const expectedMs = (when - 100 + mod.STEM_HANDOFF_RETIRE_AFTER_SEC) * 1000;
	assert.ok(Math.abs(timers[1].ms - expectedMs) < 1e-6, `retire timer ${timers[1].ms}ms, expected ${expectedMs}ms`);
	timers[1].run();
	assert.deepEqual(log.at(-1), ['retire', 'mix']);
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

test('the binding puts the mix back as the control clock says when a command lands during its stop', async () => {
	const { port, log, mix, runtime } = landing();
	mix.stop = async (when) => {
		log.push(['mix.stop', when]);
		runtime.nextScheduleRevision += 1;
	};
	assert.equal(await mod.landStemsOnDeck(port), 'deferred');
	const when = log.find((entry) => entry[0] === 'mix.stop')[1];
	const restore = log.find((entry) => entry[0] === 'mix.schedule');
	assert.ok(restore !== undefined, 'the mix was not put back');
	assert.equal(restore[1], when);
	assert.ok(Math.abs(restore[2].start - (10 + (when - 90) * 1.25)) < 1e-9, 'the mix was put back at the wrong position');
	assert.ok(names(log).includes('stems.stop'), 'the stems start was not canceled');
	assert.ok(!names(log).includes('commit'));
});

test('the binding cuts a mix that refused its stop at the handoff instant and reports it', async () => {
	const reported = [];
	const { port, log, mix, timers } = landing({ autoTimers: false, port: { outgoingFailed: (error, terminal) => reported.push([error.message, terminal]) } });
	mix.stop = async (when) => {
		log.push(['mix.stop', when]);
		throw new Error('stop refused');
	};
	assert.equal(await mod.landStemsOnDeck(port), 'handed_off');
	const when = log.find((entry) => entry[0] === 'mix.stop')[1];
	assert.deepEqual(reported, [['stop refused', false]]);
	const retireAt = timers.at(-1);
	assert.ok(Math.abs(retireAt.ms - (when - 100) * 1000) < 1e-6, 'the dead mix is not cut at the handoff instant');
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
});
