/**
 * AGENT-24: the page's transition-plan executor (src/lib/rb/agent-plan.ts),
 * driven tick by tick against a fake clock deck. Nothing about the runner is
 * mocked: only its deps (dispatch, the state it reads, the timer) are fakes,
 * and the clock deck's playhead is moved by hand between ticks.
 *
 * [if] the clock deck crosses an event's bar [then ⛔] that command fires.
 * [if] a ramp is due [then ⛔] it moves from the live value by the playhead.
 * [if] the clock deck is silent, or the plan's start has passed [then ⛔]
 *   the plan is refused before anything is dispatched.
 * [if] abort / finish_now / extend arrive mid-plan [then ⛔] they act.
 * [if] the clock deck loses its track or stops early [then ⛔] the plan fails.
 * [if] the incoming deck is paused after its play fired [then ⛔] the plan fails.
 * [if] an abort lands while the launch is in flight, or a finish_now dispatch fails
 *   [then ⛔] the launch is cancelled, or the plan ends failed.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const plan = await loadTypeScriptModule('src/lib/rb/agent-plan.ts');

const BAR_MS = 2000; // 120 BPM
const GRID = Array.from({ length: 64 * 4 }, (_, i) => ({ n: (i % 4) + 1, time_ms: i * 500 }));

function rig({
	clockBar = 10,
	clockPlaying = true,
	incoming = { stable_id: 'trk-in', playing: false },
	snapshots = false,
	landsDownbeat = true, // an armed launch starts when the playhead moves past its downbeat, as the page does
	gate = null, // holds a quantized launch in flight: it arms only once the gate opens, as the page does
	refuse = null, // a command the bus refuses: its dispatch throws
	onDispatch = null, // a hook that runs after each dispatch, so a test can change the decks mid-tick
	handingOff = () => false, // AutoPlay reports a handoff in flight: schedule refuses while it does
	hold = null, // (command, state) => a promise the dispatch waits on AFTER its effect lands, or null: a command still in flight
	stemsSilent = {} // deck => true when its ready stem parts are all muted (the page answers with everyStemPartSilent)
} = {}) {
	const dispatched = [];
	const lag = { hideArmed: false, holdWrites: false };
	// The bus's deferred mirror writes (AGENT-30): a scheduled fader or eq lands in the mirror once the audio clock
	// reaches it (unless `lag.holdWrites` delays the timer); a plain set on that control cancels it, as the engine does.
	// The engine's step states (AGENT-30, Sol P1 #7594): waiting until the write lands, landed after, cancelled by a
	// plain set on the control. The runner reads them through `stepState`, never the mirror.
	let deferred = [];
	const stepStates = new Map();
	const forgotten = [];
	const keyOf = (c) => `${c.type}:${c.deck}:${c.band ?? ''}`;
	/** A plain set on a control, as the bus applies it: it cancels the steps waiting there, then moves the mirror. */
	const plainSet = (command) => {
		for (const w of deferred.filter((w) => keyOf(w) === keyOf(command))) stepStates.set(w.step_id, 'cancelled');
		deferred = deferred.filter((w) => keyOf(w) !== keyOf(command));
		if (command.type === 'fader') state.mixer.channels[command.deck].fader = command.value;
		if (command.type === 'eq') state.mixer.channels[command.deck][`eq_${command.band}`] = command.value;
	};
	const state = {
		master_deck: 1,
		master: { muted: false },
		decks: {
			1: { stable_id: 'trk-out', playing: clockPlaying, position_ms: (clockBar - 1) * BAR_MS, beatgrid: GRID, pitch: 1, pitch_range: 8, effective_bpm: 120 },
			2: { stable_id: incoming.stable_id, playing: incoming.playing, position_ms: 0, beatgrid: GRID, pitch: 1, pitch_range: 8, effective_bpm: 120 },
			3: { stable_id: null, playing: false, position_ms: 0, beatgrid: [], pitch: 1 },
			4: { stable_id: null, playing: false, position_ms: 0, beatgrid: [], pitch: 1 }
		},
		mixer: {
			crossfader: 0.5,
			master: 1,
			channels: {
				1: { fader: 1, trim: 0.5, assign: 'THRU', eq_low: 0.5 },
				2: { fader: 0, trim: 0.5, assign: 'THRU', eq_low: 0 },
				3: {},
				4: {}
			}
		}
	};
	let now = 0;
	let anchor; // undefined: follow the deck; a test pins the engine's observation to tell it from the live clock
	function queryState() {
		if (!lag.holdWrites) {
			const due = deferred.filter((w) => w.at_context_sec <= 1000 + now / 1000);
			deferred = deferred.filter((w) => !due.includes(w));
			for (const w of due) {
				if (w.type === 'fader') state.mixer.channels[w.deck].fader = w.value;
				else state.mixer.channels[w.deck][`eq_${w.band}`] = w.value;
				stepStates.set(w.step_id, 'landed');
			}
		}
		// The mirror can trail the engine: `lag.hideArmed` hides a launch the engine has armed.
		if (lag.hideArmed) return { ...state, decks: { ...state.decks, 2: { ...state.decks[2], quantized_launch_armed: null } } };
		return snapshots ? structuredClone(state) : state;
	}
	const runner = plan.createPlanRunner({
		dispatch: async (command) => {
			dispatched.push(command);
			if (refuse !== null && refuse(command)) throw new Error('bus refused the command');
			if (gate !== null && command.type === 'play' && command.quantize === true) await gate;
			// The bus mirrors a scheduled step only when it lands (AGENT-30), so a step sent ahead leaves the mirror as is.
			const lands = command.at_context_sec === undefined;
			if ((command.type === 'fader' || command.type === 'eq') && lands) plainSet(command);
			if ((command.type === 'fader' || command.type === 'eq') && !lands) {
				deferred.push(command);
				stepStates.set(command.step_id, 'waiting');
			}
			if (command.type === 'crossfader') state.mixer.crossfader = command.value;
			if (command.type === 'master') state.master_deck = command.deck;
			if (command.type === 'master_mute') state.master.muted = command.muted;
			if (command.type === 'play') _transport(state.decks[command.deck], command);
			if (onDispatch !== null) onDispatch(command, state, plainSet);
			const held = hold === null ? null : hold(command, state);
			if (held !== null) await held;
		},
		// The page's query builds a fresh snapshot each call; `snapshots` copies like it does.
		query: () => queryState(),

		setTimer: () => 'timer', // ticks are driven by hand
		clearTimer: () => {},
		now: () => now,
		handoffInFlight: handingOff,
		// The AudioContext clock: 1000 s ahead of the page clock, so a test can tell the two apart.
		contextNowSec: () => 1000 + now / 1000,
		// One engine observation: the clock deck's playhead with the context time it was presented at.
		// The engine drops a finished state only when the runner forgets it (Sol P1 #7594); a waiting one never.
		forgetStep: (id) => {
			forgotten.push(id);
			if (stepStates.get(id) !== 'waiting') stepStates.delete(id);
		},
		stepState: (id) => {
			queryState(); // the bus drains due writes before anyone reads a state, as the page's timer would have
			return stepStates.get(id) ?? null;
		},
		positionAnchor: (deck) => (anchor === undefined ? { position_ms: state.decks[deck].position_ms, context_sec: 1000 + now / 1000, pitch: state.decks[deck].pitch } : anchor),
		stemsSilent: (deck) => stemsSilent[deck] === true
	});
	// Moving the playhead lands any armed launch on its downbeat, as the page does before the next tick.
	const at = (bar) => {
		state.decks[1].position_ms = (bar - 1) * BAR_MS;
		const incomingDeck = state.decks[2];
		if (landsDownbeat && incomingDeck.quantized_launch_armed != null) {
			incomingDeck.quantized_launch_armed = null;
			incomingDeck.playing = true;
		}
	};
	/** A graph rebuild: the engine session ends, its pending automation and step states with it. */
	const resetEngine = () => {
		deferred = [];
		for (const [id, st] of stepStates) if (st === 'waiting') stepStates.set(id, 'lost');
	};
	return { runner, state, dispatched, at, lag, plainSet, stepStates, forgotten, resetEngine, advance: (ms) => (now += ms), pinAnchor: (value) => (anchor = value) };
}

/** The page's play semantics: quantize arms, an unquantized press on an armed deck disarms, else it plays. */
function _transport(deck, command) {
	if (!command.playing) Object.assign(deck, { playing: false, quantized_launch_armed: null });
	else if (command.quantize === true) deck.quantized_launch_armed = { remaining_ms: 250 };
	else if (deck.quantized_launch_armed) deck.quantized_launch_armed = null;
	else deck.playing = true;
}

function order(overrides = {}) {
	return {
		id: 'plan-test',
		clock: { deck: 1, stable_id: 'trk-out' },
		incoming: { deck: 2, stable_id: 'trk-in' },
		start_bar: 20,
		bars: 8,
		style: 'bass_swap_blend',
		notes: [],
		prepare: [{ type: 'seek', deck: 2, position_ms: 0 }],
		events: [
			{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
			{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
			{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
			{ at_bar: 8, command: { type: 'master', deck: 2 } },
			{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
		],
		...overrides
	};
}

test('barAt counts 1-based bars on downbeats, like apps/autodj/profile.py', () => {
	const downbeats = plan.downbeatsMs(GRID);
	assert.equal(downbeats.length, 64);
	assert.equal(plan.barAt(downbeats, 0), 1);
	assert.equal(plan.barAt(downbeats, 3 * BAR_MS + 1000), 4.5);
	assert.throws(() => plan.barAt([0], 10), /no_grid/);
});

test('schedule runs prepare at once and fires nothing before its bar', async () => {
	const { runner, dispatched } = rig({ clockBar: 10 });
	const armed = await runner.schedule(order());
	assert.equal(armed.status, 'scheduled');
	assert.equal(armed.bar, -10);
	assert.deepEqual(dispatched, [{ type: 'seek', deck: 2, position_ms: 0 }]);
	await runner.tick();
	assert.equal(dispatched.length, 1);
	assert.equal(runner.mirror().status, 'scheduled');
});

test('an outgoing close waits while the incoming launch is armed but not yet playing', async () => {
	// Both channels are open, so before the launch lands the room is heard on the incoming side only once it plays.
	// The clock closes its fader at bar 2, while the launch is still armed: the close must wait for the launch.
	const { runner, state, dispatched, at } = rig({ clockBar: 19, landsDownbeat: false });
	state.mixer.channels[2].fader = 1;
	const scheduled = order({
		bars: 4,
		events: [
			{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
			{ at_bar: 2, command: { type: 'fader', deck: 1, value: 0 } }
		]
	});
	await runner.schedule(scheduled);
	at(19.8); // plan bar -0.2: the quantized play at -0.25 is due
	await runner.tick(); // the play fires and arms the launch
	assert.equal(state.decks[2].quantized_launch_armed != null, true);
	at(22.5); // plan bar 2.5: the close is due, but the launch has not landed
	await runner.tick();
	assert.equal(runner.mirror().status, 'running', 'the plan waits rather than failing');
	assert.equal(dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), false, 'the close is held back');
	// The launch lands on its downbeat; the next pass runs the close.
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), true, 'the close runs once the launch plays');
});

test('events fire as the clock crosses their bars and ramps follow the playhead', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8); // plan bar -0.2: the quantized play is due (-0.25)
	await runner.tick();
	assert.deepEqual(dispatched.at(-1), { type: 'play', deck: 2, playing: true, quantize: true });
	assert.equal(runner.mirror().status, 'running');
	at(22); // plan bar 2: the fader ramp is half way from 0 to 1
	await runner.tick();
	assert.equal(state.mixer.channels[2].fader, 0.5);
	at(24.5); // plan bar 4.5: ramp done, the bass kill fired
	await runner.tick();
	assert.equal(state.mixer.channels[2].fader, 1);
	assert.ok(dispatched.some((c) => c.type === 'eq' && c.deck === 1 && c.value === 0));
	at(28.1);
	await runner.tick();
	assert.ok(dispatched.some((c) => c.type === 'master' && c.deck === 2));
	assert.ok(dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false));
	const done = runner.mirror();
	assert.equal(done.status, 'done');
	assert.equal(done.fired, done.total);
});

test('a later command on the same control cancels a running ramp', async () => {
	const { runner, state, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'crossfader', value: 1 }, to: 1, over_bars: 4 } },
				{ at_bar: 1, command: { type: 'crossfader', value: 0.2 } },
				{ at_bar: 3, command: { type: 'master', deck: 2 } }
			]
		})
	);
	at(20.1);
	await runner.tick();
	at(21.5);
	await runner.tick();
	assert.equal(state.mixer.crossfader, 0.2);
	at(22.5);
	await runner.tick();
	assert.equal(state.mixer.crossfader, 0.2, 'the cancelled ramp must not move the crossfader again');
});

test('a quantized incoming launch is refused when the master has moved off the clock deck', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	state.master_deck = 2; // a visible control elects the other deck after prepare
	at(19.8); // plan bar -0.2: the quantized play is due
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.quantize === true), false);
	assert.equal(runner.mirror().status, 'failed');
});

test('a tempo ramp whose target the narrowed pitch range no longer allows fails before any step', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [{ at_bar: 0, ramp: { command: { type: 'tempo', deck: 2, ratio: 1.05 }, to: 1.05, over_bars: 4 } }]
		})
	);
	state.decks[2].pitch_range = 2; // the operator narrows deck 2 to +-2% before the ramp starts
	at(20.1);
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'tempo'), false, 'no step may move the deck partway');
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /outside deck 2's pitch range/);
});

test('a master the operator elects during prepare fails the plan, and no later prepare command runs', async () => {
	const { runner, dispatched } = rig({
		clockBar: 19,
		onDispatch: (command, state) => {
			if (command.type === 'seek') state.master_deck = 2; // another playing deck is promoted mid-prepare
		}
	});
	await assert.rejects(
		runner.schedule(
			order({
				prepare: [
					{ type: 'seek', deck: 2, position_ms: 0 },
					{ type: 'seek', deck: 2, position_ms: 2000 }
				]
			})
		),
		/master moved to deck 2 during prepare/
	);
	assert.equal(dispatched.filter((c) => c.type === 'seek').length, 1, 'the second prepare command must not run');
	assert.equal(dispatched.some((c) => c.type === 'play'), false, 'the plan must not arm');
	assert.equal(runner.mirror().status, 'failed');
});

test('an outgoing master the operator re-elects after prepare made the clock deck master is refused, and no later prepare command runs', async () => {
	const { runner, state, dispatched } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (command.type === 'master') current.master_deck = command.deck; // prepare promotes the clock deck
			if (command.type === 'seek' && command.position_ms === 0) current.master_deck = 2; // the operator re-elects the old master while this seek is awaited
		}
	});
	state.master_deck = 2;
	await assert.rejects(
		runner.schedule(
			order({
				prepare: [
					{ type: 'master', deck: 1 },
					{ type: 'seek', deck: 2, position_ms: 0 },
					{ type: 'seek', deck: 2, position_ms: 2000 }
				]
			})
		),
		/master moved to deck 2 during prepare/
	);
	assert.equal(dispatched.filter((c) => c.type === 'seek').length, 1, 'the second prepare command must not run');
	assert.equal(runner.mirror().status, 'failed');
});

test('a plan with no master prepare command is refused before any dispatch when the clock deck is not master', async () => {
	const { runner, state, dispatched } = rig({ clockBar: 19 });
	state.master_deck = 2;
	await assert.rejects(runner.schedule(order()), /clock deck 1 is not master/);
	assert.equal(dispatched.length, 0);
});

test('an incoming deck the operator starts after schedule, before the plan play fires, fails the plan', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	state.decks[2].playing = true; // the operator starts the incoming deck once schedule has returned
	at(19.8); // plan bar -0.2: the quantized play is due
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.quantize === true), false);
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /started before the plan's play/);
});

test('an incoming deck the operator starts during an earlier event dispatch in the same tick fails the plan before its play', async () => {
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (command.type === 'trim') current.decks[2].playing = true; // the operator starts deck 2 while the trim is awaited
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'trim', deck: 1, value: 0.5 } },
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } }
			]
		})
	);
	at(19.8); // plan bar -0.2: the trim and the quantized play are both due
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.quantize === true), false);
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /started before the plan's play/);
	assert.equal(state.decks[2].playing, true);
});

test('a plan whose last step is a launch stays running until the launch lands', async () => {
	const { runner, state, at } = rig({ clockBar: 19, landsDownbeat: false });
	await runner.schedule(
		order({ events: [{ at_bar: 0, command: { type: 'play', deck: 2, playing: true, quantize: true } }] })
	);
	at(20.5); // plan bar 0.5: the launch fired and is armed for its downbeat, the last step
	await runner.tick();
	assert.equal(runner.mirror().status, 'running');
	state.decks[2].quantized_launch_armed = null; // the downbeat lands
	state.decks[2].playing = true;
	await runner.tick();
	assert.equal(runner.mirror().status, 'done');
});

test('a delayed tick settles a ramp before a later ramp on its control starts, so the later one starts where it lands', async () => {
	const { runner, state, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
				{ at_bar: 4, ramp: { command: { type: 'fader', deck: 2, value: 0 }, to: 0, over_bars: 4 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } }
			]
		})
	);
	at(26); // plan bar 6 in one delayed tick: the first ramp has landed at 1, the second is half way down from it
	await runner.tick();
	assert.equal(state.mixer.channels[2].fader, 0.5);
});

test('a clock stop waits while the incoming launch is only armed, and fires once the downbeat lands', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	state.mixer.channels[2].fader = 1;
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(20); // plan bar 0 in one delayed tick: the launch is armed for its downbeat and the clock stop is due
	await runner.tick();
	assert.ok(dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.quantize === true));
	assert.equal(
		dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false),
		false,
		'the outgoing deck must hold the room until the launch lands'
	);
	assert.equal(runner.mirror().status, 'running');
	state.decks[2].quantized_launch_armed = null; // the downbeat lands
	state.decks[2].playing = true;
	at(20.5);
	await runner.tick();
	assert.ok(dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false));
	assert.equal(runner.mirror().status, 'done');
});

test('schedule refuses an incoming deck already armed to launch, and only that', async () => {
	const armed = rig();
	armed.state.decks[2].quantized_launch_armed = { remaining_ms: 250 };
	const dispatched = armed.dispatched;
	await assert.rejects(() => armed.runner.schedule(order()), /already armed to launch/);
	assert.equal(dispatched.length, 0, 'a refused plan dispatches nothing');
	// Control: the same paused deck with nothing armed schedules.
	const idle = rig();
	assert.equal((await idle.runner.schedule(order())).status, 'scheduled');
});

test('schedule refuses while an AutoPlay handoff is in flight, and accepts once it lands', async () => {
	let handingOff = true;
	const busy = rig({ handingOff: () => handingOff });
	await assert.rejects(() => busy.runner.schedule(order()), /autoplay_handoff/);
	assert.equal(busy.dispatched.length, 0, 'a refused plan dispatches nothing');
	handingOff = false;
	assert.equal((await busy.runner.schedule(order())).status, 'scheduled');
});

test('an incoming deck the operator starts during a prepare command gets no later prepare command', async () => {
	let started = false;
	const taken = rig({
		onDispatch: (command, state) => {
			if (command.type === 'seek' && command.deck === 2 && !started) {
				started = true;
				state.decks[2].playing = true; // the operator starts it while the first seek is awaited
			}
		}
	});
	const prepare = [
		{ type: 'seek', deck: 2, position_ms: 0 },
		{ type: 'seek', deck: 2, position_ms: 1000 }
	];
	await assert.rejects(() => taken.runner.schedule(order({ prepare })), /already playing/);
	const seeks = taken.dispatched.filter((command) => command.type === 'seek' && command.deck === 2);
	assert.equal(seeks.length, 1, 'the second prepare seek must not move the deck the operator started');
	assert.equal(taken.dispatched.some((command) => command.type === 'play'), false, 'nothing is armed after the refusal');
});

test('a clock deck the operator stops during a prepare command gets no later prepare command', async () => {
	let stopped = false;
	const lost = rig({
		onDispatch: (command, state) => {
			if (command.type === 'seek' && command.deck === 2 && !stopped) {
				stopped = true;
				state.decks[1].playing = false; // the operator stops the clock while the first seek is awaited
			}
		}
	});
	const prepare = [
		{ type: 'seek', deck: 2, position_ms: 0 },
		{ type: 'seek', deck: 2, position_ms: 1000 }
	];
	await assert.rejects(() => lost.runner.schedule(order({ prepare })), /must be playing/);
	const seeks = lost.dispatched.filter((command) => command.type === 'seek' && command.deck === 2);
	assert.equal(seeks.length, 1, 'the second prepare seek must not run once the clock has stopped');
	assert.equal(lost.dispatched.some((command) => command.type === 'play'), false, 'nothing is armed after the refusal');
});

test('schedule refuses a silent clock, a passed start, a busy runner and a live incoming deck', async () => {
	await assert.rejects(() => rig({ clockPlaying: false }).runner.schedule(order()), /clock_not_playing/);
	await assert.rejects(() => rig({ clockBar: 25 }).runner.schedule(order()), /start_passed/);
	await assert.rejects(
		() => rig({ incoming: { stable_id: 'trk-in', playing: true } }).runner.schedule(order()),
		/already playing/
	);
	await assert.rejects(() => rig().runner.schedule(order({ start_bar: 60 })), /past the clock deck's grid/);
	const { runner, dispatched } = rig();
	await runner.schedule(order());
	await assert.rejects(() => runner.schedule(order({ id: 'plan-two' })), /plan_busy/);
	assert.equal(dispatched.length, 1, 'a refused plan dispatches nothing');
	// control: a well-formed plan on a playing deck IS accepted (rig() above)
	assert.equal(runner.mirror().id, 'plan-test');
});

test('parsePlanOrder refuses commands a plan may not carry', () => {
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 0, command: { type: 'load', deck: 2, stable_id: 'x' } }] })),
		/type must be one of/
	);
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 0, ramp: { command: { type: 'master', deck: 2 }, to: 1, over_bars: 1 } }] })),
		/ramp.command.type/
	);
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 0, ramp: { command: { type: 'tempo', deck: 2, ratio: 1 }, to: 0, over_bars: 1 } }] })),
		/out of range/
	);
});

test('parsePlanOrder refuses a timed command on a deck the plan never named', () => {
	// Control: the same stop on the clock deck, which the plan does name, parses.
	plan.parsePlanOrder(order({ events: [{ at_bar: 1, command: { type: 'play', deck: 1, playing: false } }] }));
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 1, command: { type: 'play', deck: 3, playing: false } }] })),
		/may touch only the clock or incoming deck/
	);
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 1, ramp: { command: { type: 'fader', deck: 4, value: 0 }, to: 0, over_bars: 1 } }] })),
		/may touch only the clock or incoming deck/
	);
});

test('parsePlanOrder refuses a plan with no incoming deck', () => {
	// Control: the same events with an incoming deck named parse.
	plan.parsePlanOrder(order({ events: [{ at_bar: 1, command: { type: 'play', deck: 1, playing: false } }] }));
	assert.throws(
		() => plan.parsePlanOrder(order({ incoming: null, events: [{ at_bar: 1, command: { type: 'play', deck: 1, playing: false } }] })),
		/plan.incoming must name the incoming deck/
	);
});

test('parsePlanOrder refuses a timed step outside the plan\'s bars', () => {
	// Control: a stop at the last bar, and a ramp ending on the plan's last bar, parse.
	plan.parsePlanOrder(
		order({
			events: [
				{ at_bar: 7, command: { type: 'play', deck: 1, playing: false } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 8 } }
			]
		})
	);
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 100, command: { type: 'play', deck: 1, playing: false } }] })),
		/outside the plan's 8 bars/
	);
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 4, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 5 } }] })),
		/runs past the plan's 8 bars/
	);
});

test('parsePlanOrder refuses setup-only verbs as timed events', () => {
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 1, command: { type: 'seek', deck: 2, position_ms: 0 } }] })),
		/is setup and belongs in prepare/
	);
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 1, command: { type: 'beat_sync', deck: 2, enabled: true } }] })),
		/is setup and belongs in prepare/
	);
});

test('parsePlanOrder lets prepare set up the incoming deck and select the clock as master, and nothing else', () => {
	// The compiler's own setup parses: a cue, a tempo, Beat Sync and a hoisted incoming set, plus the clock master.
	plan.parsePlanOrder(
		order({
			prepare: [
				{ type: 'master', deck: 1 },
				{ type: 'seek', deck: 2, position_ms: 0 },
				{ type: 'beat_sync', deck: 2, enabled: true },
				{ type: 'fader', deck: 2, value: 0 }
			]
		})
	);
	// A transport play or a clock-side set in prepare would stop the outgoing room before any tick sees it.
	assert.throws(() => plan.parsePlanOrder(order({ prepare: [{ type: 'play', deck: 1, playing: false }] })), /type must be one of/);
	assert.throws(() => plan.parsePlanOrder(order({ prepare: [{ type: 'fader', deck: 1, value: 0 }] })), /may set only the incoming deck/);
	assert.throws(() => plan.parsePlanOrder(order({ prepare: [{ type: 'master', deck: 2 }] })), /select only the clock deck/);
});

test('abort stops a running plan and dispatches nothing more', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(20.5);
	await runner.tick();
	Object.assign(state.decks[2], { playing: true, quantized_launch_armed: null }); // its launch landed on bar 0
	const before = dispatched.length;
	const result = await runner.amend({ id: 'plan-test', action: 'abort' });
	assert.equal(result.status, 'aborted');
	at(28.5);
	await runner.tick();
	assert.equal(dispatched.length, before);
	await assert.rejects(() => runner.amend({ id: 'plan-test', action: 'abort' }), /already aborted/);
});

test('finish_now applies every remaining step, ramps landing on their targets', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(21);
	await runner.tick();
	const result = await runner.amend({ id: 'plan-test', action: 'finish_now' });
	assert.equal(result.status, 'done');
	assert.equal(state.mixer.channels[2].fader, 1);
	assert.ok(dispatched.some((c) => c.type === 'master' && c.deck === 2));
	assert.ok(dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false));
});

test('finish_now lets a later authored set win over an active ramp on the same control', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	const events = [
		...order().events.slice(0, 2),
		{ at_bar: 6, command: { type: 'fader', deck: 2, value: 0.5 } },
		...order().events.slice(2)
	];
	await runner.schedule(order({ events }));
	at(21);
	await runner.tick();
	const result = await runner.amend({ id: 'plan-test', action: 'finish_now' });
	assert.equal(result.status, 'done');
	const faderSets = dispatched.filter((c) => c.type === 'fader' && c.deck === 2).map((c) => c.value);
	assert.deepEqual(faderSets.slice(-2), [1, 0.5], 'the ramp lands first, then the authored set');
	assert.equal(state.mixer.channels[2].fader, 0.5);
});

test('extend pushes unfired events later and refuses to run past the grid', async () => {
	const { runner, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(20.5);
	await runner.tick();
	const result = await runner.amend({ id: 'plan-test', action: 'extend', bars: 8 });
	assert.equal(result.bars, 16);
	at(28.5); // the original handover bar: nothing hands over yet
	await runner.tick();
	assert.ok(!dispatched.some((c) => c.type === 'master'));
	at(36.1);
	await runner.tick();
	assert.ok(dispatched.some((c) => c.type === 'master' && c.deck === 2));
	const { runner: other } = rig({ clockBar: 19 });
	await other.schedule(order());
	await assert.rejects(() => other.amend({ id: 'plan-test', action: 'extend', bars: 40 }), /extend_past_grid/);
});

test('an extend arriving while a tick awaits a dispatch waits for that tick, then judges what it left', async () => {
	for (const extendMidTick of [true, false]) {
		const { runner, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(20.5);
		await runner.tick();
		const original = dispatched.push.bind(dispatched);
		let extendAttempt = null;
		dispatched.push = (command) => {
			const n = original(command);
			// The handover's master promotion is out; the extend queues behind the tick (one transaction at a time) and is
			// refused once it runs, since the tick finished the handover and the plan.
			if (extendMidTick && command.type === 'master') extendAttempt = runner.amend({ id: 'plan-test', action: 'extend', bars: 8 });
			return n;
		};
		at(28.5);
		await runner.tick();
		const stopped = dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false);
		// The handover stop still goes out, whether or not the extend arrived mid-tick.
		assert.equal(stopped, true);
		if (extendMidTick) {
			await assert.rejects(extendAttempt, /already done/);
			assert.notEqual(runner.mirror().bars, 16);
		}
	}
});

test('extend counts the incoming grid at the beat-sync fold, and refuses when the tempos are unknown', async () => {
	for (const [incomingBpm, refused] of [
		[240, true], // a 2x fold: the 16 clock bars left consume 32 incoming bars, past its grid from bar 40
		[120, false] // control: no fold, the same 16 bars fit
	]) {
		const { runner, state, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(20.5);
		await runner.tick();
		state.decks[2].playing = true;
		state.decks[2].effective_bpm = incomingBpm;
		state.decks[2].position_ms = (40 - 1) * BAR_MS;
		const extend = runner.amend({ id: 'plan-test', action: 'extend', bars: 8 });
		if (refused) await assert.rejects(() => extend, /extend_past_grid: 8 more bars run past the incoming deck's grid/);
		else assert.equal((await extend).bars, 16);
	}
	const { runner, state, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(20.5);
	await runner.tick();
	state.decks[2].playing = true;
	state.decks[2].effective_bpm = null;
	await assert.rejects(
		() => runner.amend({ id: 'plan-test', action: 'extend', bars: 8 }),
		/the decks report no tempo, so the incoming grid cannot be measured/
	);
});

test('extend bounds the grid by the incoming tempo ramps still to come, not only the current tempo', async () => {
	for (const [target, refused] of [
		[1.25, true], // 15.5 clock bars at 1.25 consume 19.4 incoming bars, past the 18 left from bar 47
		[1.05, false] // control: 15.5 bars at 1.05 consume 16.3 and fit under the last downbeat
	]) {
		const ramped = order({
			events: [...order().events, { at_bar: 4, ramp: { command: { type: 'tempo', deck: 2, ratio: target }, to: target, over_bars: 4 } }]
		});
		const { runner, state, at } = rig({ clockBar: 19 });
		await runner.schedule(ramped);
		at(20.5);
		await runner.tick();
		state.decks[2].playing = true;
		state.decks[2].position_ms = (47 - 1) * BAR_MS;
		const extend = runner.amend({ id: 'plan-test', action: 'extend', bars: 8 });
		if (refused) await assert.rejects(() => extend, /extend_past_grid: 8 more bars run past the incoming deck's grid/);
		else assert.equal((await extend).bars, 16);
	}
});

test('extend refuses to run a playing incoming deck past its own grid', async () => {
	for (const [incomingBar, refused] of [
		[60, true], // 60 + 15.5 bars left runs past its 64 downbeats
		[2, false] // control: an incoming deck early in its track extends
	]) {
		const { runner, state, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(20.5);
		await runner.tick();
		state.decks[2].playing = true;
		state.decks[2].position_ms = (incomingBar - 1) * BAR_MS;
		const extend = runner.amend({ id: 'plan-test', action: 'extend', bars: 8 });
		if (refused) await assert.rejects(() => extend, /extend_past_grid: 8 more bars run past the incoming deck's grid/);
		else assert.equal((await extend).bars, 16);
	}
});

test('the plan fails when the clock deck loses its track or stops early', async () => {
	const lost = rig({ clockBar: 19 });
	await lost.runner.schedule(order());
	lost.state.decks[1].stable_id = 'trk-other';
	await lost.runner.tick();
	assert.equal(lost.runner.mirror().status, 'failed');
	assert.match(lost.runner.mirror().reason, /no longer holds/);

	const stopped = rig({ clockBar: 19 });
	await stopped.runner.schedule(order());
	stopped.at(22);
	stopped.state.decks[1].playing = false;
	await stopped.runner.tick();
	assert.equal(stopped.runner.mirror().status, 'failed');
});

test('a clock deck that runs out in the last bar completes the handover', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(27.5); // plan bar 7.5 of 8
	state.decks[1].playing = false;
	await runner.tick();
	assert.equal(runner.mirror().status, 'done');
	assert.ok(dispatched.some((c) => c.type === 'master' && c.deck === 2));
});

test('the mirror forgets a finished plan after the keep window', async () => {
	const { runner, advance } = rig({ clockBar: 19 });
	await runner.schedule(order());
	await runner.amend({ id: 'plan-test', action: 'abort' });
	assert.equal(runner.mirror().status, 'aborted');
	advance(plan.PLAN_MIRROR_KEEP_MS + 1);
	assert.equal(runner.mirror(), null);
});

// AGENT-25: a muted agent session mutes the master in prepare; a plan never unmutes.
test('prepare may mute the master, and a plan may never unmute it or mute on a bar', async () => {
	const mute = { type: 'master_mute', muted: true, persist: false };
	const { runner, dispatched } = rig({ clockBar: 10 });
	await runner.schedule(order({ prepare: [mute, { type: 'seek', deck: 2, position_ms: 0 }] }));
	assert.deepEqual(dispatched[0], mute);
	assert.throws(
		() => plan.parsePlanOrder(order({ prepare: [{ type: 'master_mute', muted: false }] })),
		/never unmute/
	);
	assert.throws(
		() => plan.parsePlanOrder(order({ events: [{ at_bar: 1, command: mute }] })),
		/plan\.events\[0\]\.command\.type must be one of/
	);
});

test('dispose cancels a fired, still-armed launch before the runner stops', async () => {
	const { runner, dispatched, at } = rig({ clockBar: 19, landsDownbeat: false });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	await runner.dispose();
	assert.equal(runner.mirror().status, 'aborted');
	assert.ok(
		dispatched.some((command) => command.type === 'play' && command.deck === 2 && command.playing === false),
		'the armed incoming launch is cancelled before teardown'
	);
});

test('dispose aborts a live plan and nothing dispatches after it', async () => {
	const { runner, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	assert.equal(runner.mirror().status, 'running');
	await runner.dispose(); // may cancel an armed launch; that one command is the last the runner sends
	const sent = dispatched.length;
	assert.equal(runner.mirror().status, 'aborted');
	assert.match(runner.mirror().reason, /performance page closed/);
	at(24.5); // the ramp and the bass kill would both be due now
	await runner.tick();
	assert.equal(dispatched.length, sent, 'a disposed runner dispatches nothing');
});

test('a tick in flight at dispose sends nothing more and keeps the aborted status', async () => {
	const { runner, dispatched, at } = rig({ clockBar: 19 });
	const twoRamps = order();
	twoRamps.events.splice(2, 0, { at_bar: 0, ramp: { command: { type: 'eq', deck: 2, band: 'low', value: 0.5 }, to: 0.5, over_bars: 4 } });
	await runner.schedule(twoRamps);
	at(19.8);
	await runner.tick();
	at(20.5); // both ramps start
	await runner.tick();
	at(22); // mid-ramp: this tick dispatches only the two ramp steps
	const original = dispatched.push.bind(dispatched);
	let rampSteps = 0;
	dispatched.push = (command) => {
		rampSteps += 1;
		const n = original(command);
		if (rampSteps === 1) runner.dispose();
		return n;
	};
	await runner.tick();
	assert.equal(rampSteps, 1, 'only the ramp step already under way went out');
	assert.equal(runner.mirror().status, 'aborted');
});

test('control: a runner that is not disposed keeps dispatching as its bars arrive', async () => {
	const { runner, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	const sent = dispatched.length;
	at(24.5);
	await runner.tick();
	assert.ok(dispatched.length > sent);
});

test('finish_now before the incoming play fired starts the incoming deck at once', async () => {
	for (const [barNow, expectQuantize] of [
		[19.5, undefined], // scheduled: the play has not fired, so finish_now starts it unquantized
		[20.5, true] // control: the quantized play already fired and its launch landed
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(barNow);
		await runner.tick();
		if (expectQuantize) Object.assign(state.decks[2], { playing: true, quantized_launch_armed: null });
		await runner.amend({ id: 'plan-test', action: 'finish_now' });
		const plays = dispatched.filter((c) => c.type === 'play' && c.deck === 2);
		assert.equal(plays.length, 1);
		assert.equal(plays[0].quantize, expectQuantize);
		assert.ok(!('quantize' in plays[0]) || expectQuantize === true);
	}
});

test('finish_now while the fired play waits for its downbeat starts the incoming deck now', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8); // the quantized play fired at -0.25 and is armed for bar 0
	await runner.tick();
	assert.ok(state.decks[2].quantized_launch_armed && !state.decks[2].playing);
	await runner.amend({ id: 'plan-test', action: 'finish_now' });
	assert.equal(state.decks[2].playing, true);
	assert.equal(runner.mirror().status, 'done');
	// One press disarms, the next plays, and both come before the outgoing deck stops.
	const plays = dispatched.map((c, i) => [c, i]).filter(([c]) => c.type === 'play');
	assert.deepEqual(
		plays.map(([c]) => [c.deck, c.playing, c.quantize]),
		[[2, true, true], [2, true, undefined], [2, true, undefined], [1, false, undefined]]
	);
});

test('abort or a failure while the fired play waits for its downbeat cancels that launch', async () => {
	for (const [barNow, end, armed] of [
		[19.8, 'abort', true],
		[19.8, 'clock_stops', true],
		[19.5, 'abort', false] // control: before the play fires, there is nothing to cancel
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(barNow);
		await runner.tick();
		assert.equal(Boolean(state.decks[2].quantized_launch_armed), armed);
		const before = dispatched.length;
		if (end === 'abort') await runner.amend({ id: 'plan-test', action: 'abort' });
		else {
			state.decks[1].playing = false;
			await runner.tick();
		}
		assert.equal(runner.mirror().status, end === 'abort' ? 'aborted' : 'failed');
		assert.equal(state.decks[2].quantized_launch_armed ?? null, null);
		assert.equal(state.decks[2].playing, false);
		assert.deepEqual(dispatched.slice(before), armed ? [{ type: 'play', deck: 2, playing: false }] : []);
	}
});

test('an abort that arrives while the quantized launch is still in flight cancels it once it lands', async () => {
	let release;
	const gate = new Promise((resolve) => (release = resolve));
	const { runner, state, dispatched, at, lag } = rig({ clockBar: 19, gate });
	await runner.schedule(order());
	at(19.8);
	const pass = runner.tick(); // the play dispatches and waits on the gate, not yet armed
	const abort = runner.amend({ id: 'plan-test', action: 'abort' });
	lag.hideArmed = true; // the launch arms on the engine; the mirror has not published it yet
	release();
	await Promise.all([pass, abort]);
	assert.equal(runner.mirror().status, 'aborted');
	assert.equal(state.decks[2].quantized_launch_armed ?? null, null, 'nothing stays armed');
	assert.equal(state.decks[2].playing, false, 'the incoming deck never starts');
	assert.deepEqual(dispatched.at(-1), { type: 'play', deck: 2, playing: false });
});

test('a dispose that arrives while the quantized launch is still in flight waits for it, then cancels it', async () => {
	let release;
	const gate = new Promise((resolve) => (release = resolve));
	const { runner, state, dispatched, at, lag } = rig({ clockBar: 19, gate });
	await runner.schedule(order());
	at(19.8);
	const pass = runner.tick(); // the play dispatches and waits on the gate, not yet armed
	const closed = runner.dispose();
	assert.equal(
		dispatched.some((command) => command.type === 'play' && command.playing === false),
		false,
		'the cancel waits for the launch in flight'
	);
	lag.hideArmed = true; // the launch arms on the engine; the mirror has not published it yet
	release();
	await Promise.all([pass, closed]);
	assert.equal(runner.mirror().status, 'aborted');
	assert.equal(state.decks[2].quantized_launch_armed ?? null, null, 'nothing stays armed once the page closes');
	assert.equal(state.decks[2].playing, false, 'the incoming deck never starts');
	assert.deepEqual(dispatched.at(-1), { type: 'play', deck: 2, playing: false });
});

test('a pause on the incoming deck after its play fired fails the plan on the next tick', async () => {
	for (const [paused, expected] of [
		[true, 'failed'],
		[false, 'running'] // control: the same tick with the incoming deck still playing
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(19.8); // the quantized play fires and arms
		await runner.tick();
		state.decks[2].quantized_launch_armed = null; // the launch lands
		state.decks[2].playing = true;
		at(20.2);
		await runner.tick();
		if (paused) state.decks[2].playing = false;
		at(21);
		const before = dispatched.length;
		await runner.tick();
		assert.equal(runner.mirror().status, expected);
		if (paused) {
			assert.match(runner.mirror().reason, /incoming deck 2 stopped mid-plan/);
			assert.ok(!dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 1), 'the outgoing deck is not stopped');
		}
	}
});

test('finish_now fails a paused incoming deck that no tick has reached yet', async () => {
	for (const [paused, expected] of [
		[true, 'failed'],
		[false, 'done'] // control: the same finish_now with the incoming deck still playing
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(19.8); // the quantized play fires and arms
		await runner.tick();
		state.decks[2].quantized_launch_armed = null; // the launch lands
		state.decks[2].playing = true;
		at(20.2);
		await runner.tick();
		if (paused) state.decks[2].playing = false; // paused after the last tick
		const before = dispatched.length;
		const finish = runner.amend({ id: 'plan-test', action: 'finish_now' });
		if (paused) await assert.rejects(() => finish, /incoming deck 2 stopped mid-plan/);
		else await finish;
		assert.equal(runner.mirror().status, expected);
		if (paused) assert.ok(!dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 1), 'the outgoing deck is not stopped');
	}
});

test('a finish_now whose dispatch fails ends the plan failed and says why', async () => {
	const { runner } = rig({ clockBar: 19, refuse: (command) => command.type === 'eq' });
	await runner.schedule(order());
	await assert.rejects(() => runner.amend({ id: 'plan-test', action: 'finish_now' }), /bus refused the command/);
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /finish_now failed: bus refused the command/);
});

test('extend counts an armed incoming deck as playing every added bar', async () => {
	for (const [armed, refused] of [
		[true, true], // armed at bar 60: the fired play is not shifted, so 8 more bars run off its grid
		[false, false] // control: the same deck with no launch armed is not bound by its grid
	]) {
		const { runner, state, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(19.8);
		await runner.tick();
		state.decks[2].position_ms = 59 * BAR_MS;
		if (!armed) state.decks[2].quantized_launch_armed = null;
		const extend = runner.amend({ id: 'plan-test', action: 'extend', bars: 8 });
		if (refused) await assert.rejects(() => extend, /extend_past_grid: 8 more bars run past the incoming deck's grid/);
		else assert.equal((await extend).bars, 16);
	}
});

test('a track swapped on the incoming deck mid-tick fails the plan before the next event fires on it', async () => {
	for (const [swap, expected] of [
		[true, 'failed'],
		[false, 'running'] // control: the same tick with the incoming track still loaded
	]) {
		const onDispatch = (command, state) => {
			if (swap && command.type === 'fader' && command.deck === 2) state.decks[2].stable_id = 'trk-other';
		};
		const { runner, dispatched, at } = rig({ clockBar: 19, onDispatch });
		await runner.schedule(order());
		at(24.5); // plan bar 4.5: the ramp's dispatch swaps the track, then the bar-4 EQ event is due
		await runner.tick();
		assert.equal(runner.mirror().status, expected);
		if (swap) {
			assert.match(runner.mirror().reason, /incoming deck 2 no longer holds trk-in/);
			assert.ok(!dispatched.some((c) => c.type === 'eq'), 'the EQ event does not fire after the swap');
		} else {
			assert.ok(dispatched.some((c) => c.type === 'eq'), 'control: the EQ event fires while the track is held');
		}
	}
});

test('the plan fails when the incoming deck loses its track before the handover', async () => {
	for (const [swapped, expected] of [
		[true, 'failed'],
		[false, 'running'] // control: the same tick with the incoming track still loaded
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		if (swapped) state.decks[2].stable_id = 'trk-other';
		at(20.5);
		await runner.tick();
		assert.equal(runner.mirror().status, expected);
		if (swapped) {
			assert.match(runner.mirror().reason, /incoming deck 2 no longer holds trk-in/);
			assert.ok(!dispatched.some((c) => c.type === 'play'), 'nothing plays the swapped track');
		}
	}
	const { runner, state } = rig({ clockBar: 19 });
	await runner.schedule(order());
	state.decks[2].stable_id = 'trk-other';
	await assert.rejects(() => runner.amend({ id: 'plan-test', action: 'finish_now' }), /plan_failed: incoming deck 2/);
	assert.equal(runner.mirror().status, 'failed');
});

test('a ramp that starts on the bar a set moved its control continues from the set value', async () => {
	for (const [withSet, expected] of [
		[true, 0.6], // set to 0.2 at bar 0, ramp to 1 over 4 bars: half way at bar 2
		[false, 0.5] // control: no set, the ramp starts from the live 0
	]) {
		const { runner, state, at } = rig({ clockBar: 19, snapshots: true });
		const events = [
			{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
			...(withSet ? [{ at_bar: 0, command: { type: 'fader', deck: 2, value: 0.2 } }] : []),
			{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
			{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
		];
		await runner.schedule(order({ events }));
		at(20.01);
		await runner.tick();
		at(22.01);
		await runner.tick();
		assert.ok(Math.abs(state.mixer.channels[2].fader - expected) < 0.01, `fader ${state.mixer.channels[2].fader}`);
	}
});

test('an authored pause of the incoming deck is not a failure, and its replay commits again', async () => {
	const events = [
		{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
		{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
		{ at_bar: 2, command: { type: 'play', deck: 2, playing: false } },
		{ at_bar: 5, command: { type: 'play', deck: 2, playing: true } },
		{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
	];
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order({ events }));
	at(19.8);
	await runner.tick();
	state.decks[2].quantized_launch_armed = null; // the launch lands
	state.decks[2].playing = true;
	for (const bar of [20.2, 22.1, 23.5, 25.1]) {
		at(bar);
		await runner.tick();
		assert.notEqual(runner.mirror().status, 'failed', `failed at bar ${bar}: ${runner.mirror().reason}`);
	}
	assert.equal(state.decks[2].playing, true, 'the replay started the incoming deck again');
	at(28.1);
	await runner.tick();
	assert.equal(runner.mirror().status, 'done');
	assert.ok(dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false), 'the handover stopped the outgoing deck');
});

test('the handover stop refuses to silence the room when the incoming deck is closed', async () => {
	const closes = {
		fader: (state) => (state.mixer.channels[2].fader = 0),
		crossfader: (state) => {
			state.mixer.channels[2].assign = 'A';
			state.mixer.crossfader = 1;
		}
	};
	for (const [how, close] of Object.entries(closes)) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(19.8);
		await runner.tick();
		state.decks[2].quantized_launch_armed = null;
		state.decks[2].playing = true;
		at(27.5);
		await runner.tick();
		close(state); // a visible control closes the incoming path before the handover
		const before = dispatched.length;
		at(28.1);
		await runner.tick();
		assert.equal(runner.mirror().status, 'failed', how);
		assert.match(runner.mirror().reason, /incoming deck 2 is silent/, how);
		assert.ok(!dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 1), `${how}: the outgoing deck is not stopped`);
	}
});

test('the handover stop refuses when every incoming stem part is muted, even with the channel open', async () => {
	// The stem law (everyStemPartSilent) answers for the deck; the plan must consult it, since the mixer channel reads open.
	for (const [silent, expected] of [
		[true, 'failed'],
		[false, 'done'] // control: the same handover with the stems open dispatches the outgoing stop and finishes
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19, stemsSilent: { 2: silent } });
		await runner.schedule(order());
		at(19.8);
		await runner.tick();
		state.decks[2].quantized_launch_armed = null;
		state.decks[2].playing = true;
		at(27.5);
		await runner.tick();
		at(28.1);
		await runner.tick();
		const stopped = dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false);
		assert.equal(stopped, !silent, silent ? 'the silent incoming stems refuse the outgoing stop' : 'the open stems dispatch it');
		assert.equal(runner.mirror().status, expected);
		if (silent) assert.match(runner.mirror().reason, /incoming deck 2 is silent/);
	}
});

test('a timed stop of the incoming deck refuses when the outgoing stem parts are all muted, even with its channel open', async () => {
	const withStop = order({
		events: [...order().events, { at_bar: 7, command: { type: 'play', deck: 2, playing: false } }]
	});
	for (const [silent, expected] of [
		[true, 'failed'],
		[false, 'running'] // control: the same stop with the outgoing stems open dispatches
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19, stemsSilent: { 1: silent } });
		await runner.schedule(withStop);
		at(19.8);
		await runner.tick();
		state.decks[2].quantized_launch_armed = null;
		state.decks[2].playing = true;
		at(26.5);
		await runner.tick();
		at(27.5);
		await runner.tick();
		const stopped = dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.playing === false);
		assert.equal(stopped, !silent, silent ? 'the silent outgoing stems refuse the stop' : 'the heard room dispatches it');
		assert.equal(runner.mirror().status, expected);
		if (silent) assert.match(runner.mirror().reason, /outgoing deck 1 is silent/);
	}
});

test('a timed stop of the incoming deck refuses to leave the room silent when the outgoing path is closed', async () => {
	const withStop = order({
		events: [...order().events, { at_bar: 7, command: { type: 'play', deck: 2, playing: false } }]
	});
	for (const [closed, expected] of [
		[true, 'failed'],
		[false, 'running'] // control: the same stop with the outgoing deck heard dispatches
	]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(withStop);
		at(19.8);
		await runner.tick();
		state.decks[2].quantized_launch_armed = null;
		state.decks[2].playing = true;
		at(26.5);
		await runner.tick();
		if (closed) state.mixer.channels[1].fader = 0; // a visible control closes the outgoing path
		at(27.5);
		await runner.tick();
		const stopped = dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.playing === false);
		assert.equal(stopped, !closed, closed ? 'the closed room refuses the stop' : 'the heard room dispatches it');
		if (closed) {
			assert.equal(runner.mirror().status, expected);
			assert.match(runner.mirror().reason, /outgoing deck 1 is silent/);
		}
	}
});

test('a refused incoming stop cancels the armed launch, so the failed plan never starts the incoming deck', async () => {
	const withStop = order({
		events: [...order().events, { at_bar: 7, command: { type: 'play', deck: 2, playing: false } }]
	});
	for (const [closed, expected] of [
		[true, 'failed'],
		[false, 'running'] // control: the same stop with the outgoing deck heard dispatches
	]) {
	const { runner, state, at } = rig({ clockBar: 19, landsDownbeat: false });
		await runner.schedule(withStop);
		at(19.8); // the quantized play fires and arms; its launch has not landed
		await runner.tick();
		assert.ok(state.decks[2].quantized_launch_armed, 'the incoming launch is armed');
		if (closed) state.mixer.channels[1].fader = 0;
		at(27.5);
		await runner.tick();
		assert.equal(runner.mirror().status, expected);
		if (closed) {
			assert.equal(state.decks[2].quantized_launch_armed, null, 'the armed launch is cancelled');
			assert.equal(state.decks[2].playing, false, 'the incoming deck never starts');
		}
	}
});

test('a stop the bus rejects is unfired, so the failed plan still cancels the armed incoming launch', async () => {
	let rejections = 0;
	const withStop = order({
		events: [...order().events, { at_bar: 7, command: { type: 'play', deck: 2, playing: false } }]
	});
	// The bus rejects the first incoming stop only; the cancel that follows is accepted.
	const refuse = (command) => command.type === 'play' && command.deck === 2 && command.playing === false && rejections++ === 0;
	const { runner, state, at } = rig({ clockBar: 19, refuse, landsDownbeat: false });
	await runner.schedule(withStop);
	at(19.8);
	await runner.tick();
	assert.ok(state.decks[2].quantized_launch_armed, 'the incoming launch is armed');
	at(27.5);
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /bus refused the command/);
	assert.equal(state.decks[2].quantized_launch_armed, null, 'the armed launch is cancelled');
	assert.equal(state.decks[2].playing, false, 'the incoming deck never starts');
});

test('extend refuses once the handover has stopped the outgoing deck, and allows it before', async () => {
	const withStop = order({
		events: [...order().events.filter((e) => e.command?.deck !== 1 || e.command?.playing !== false), { at_bar: 7, command: { type: 'play', deck: 1, playing: false } }]
	});
	for (const [stopped, expected] of [
		[true, /extend_after_handover/],
		[false, null] // control: the same extend before the handover stop fires
	]) {
		const { runner, state, at } = rig({ clockBar: 19 });
		await runner.schedule(withStop);
		at(19.8);
		await runner.tick();
		state.decks[2].quantized_launch_armed = null;
		state.decks[2].playing = true;
		state.mixer.channels[2].fader = 1;
		at(26.5);
		await runner.tick();
		if (stopped) {
			at(27.2); // the handover stop at plan bar 7 fires on this tick
			await runner.tick();
			assert.equal(state.decks[1].playing, false, 'the outgoing deck is stopped');
		}
		const amend = runner.amend({ id: 'plan-test', action: 'extend', bars: 1 });
		if (expected) await assert.rejects(amend, expected);
		else await amend;
	}
});

test('finish_now refuses the handover while the incoming deck is closed, and the outgoing deck keeps playing', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	at(25.5); // past the fader ramp, which settles to its target on finish_now
	await runner.tick();
	state.mixer.channels[2].fader = 0; // closed by a visible control after the ramp
	const before = dispatched.length;
	await assert.rejects(() => runner.amend({ id: 'plan-test', action: 'finish_now' }), /incoming deck 2 is silent/);
	assert.equal(runner.mirror().status, 'failed');
	assert.ok(!dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 1), 'the outgoing deck is not stopped');
});


test('a ramp that would close the outgoing path is refused while the incoming deck is silent, on a tick or a finish_now', async () => {
	// The outgoing fader falls to 0 over six bars; the incoming fader's own ramp is done by bar 4, then a visible
	// control closes the incoming fader. The outgoing ramp must stop before it reaches the silent floor.
	const withFall = order({
		events: [
			...order().events,
			{ at_bar: 0, ramp: { command: { type: 'fader', deck: 1 }, to: 0, over_bars: 6 } }
		]
	});
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(withFall);
	at(19.8);
	await runner.tick();
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	at(25.5);
	await runner.tick();
	state.mixer.channels[2].fader = 0;
	const before = dispatched.length;
	at(26.5); // the outgoing ramp reaches 0 here, before the handover stop at bar 8
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /outgoing deck 1 would go silent/);
	assert.ok(!dispatched.slice(before).some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the fall is not dispatched');
	assert.ok(state.mixer.channels[1].fader > 0, 'the outgoing fader keeps its last audible level');

	const finish = rig({ clockBar: 19 });
	await finish.runner.schedule(withFall);
	finish.at(19.8);
	await finish.runner.tick();
	finish.state.decks[2].quantized_launch_armed = null;
	finish.state.decks[2].playing = true;
	finish.at(25.5);
	await finish.runner.tick();
	finish.state.mixer.channels[2].fader = 0;
	const settled = finish.dispatched.length;
	await assert.rejects(() => finish.runner.amend({ id: 'plan-test', action: 'finish_now' }), /would go silent/);
	assert.ok(!finish.dispatched.slice(settled).some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'finish_now does not settle the fall');
	assert.ok(finish.state.mixer.channels[1].fader > 0, 'the outgoing fader keeps its level');
});

test('a timed outgoing close before the incoming play is refused: the room has no incoming path yet', async () => {
	// The outgoing fader closes at plan bar -0.5, before the incoming play at -0.25 ever fires. Control: the same
	// close after the play has fired is not refused by this guard; the next test covers the closed-incoming case.
	const earlyClose = order({
		events: [{ at_bar: -0.5, command: { type: 'fader', deck: 1, value: 0 } }, ...order().events]
	});
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(earlyClose);
	at(19.6);
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /outgoing deck 1 would go silent: incoming deck 2 has not started yet/);
	assert.ok(!dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the close is not dispatched');
	assert.ok(state.mixer.channels[1].fader > 0, 'the outgoing fader keeps its level');
});

test('a timed set that would close the outgoing path is refused while the incoming deck is silent', async () => {
	// Same timeline as the ramp refusal, but the fall is one timed set of the outgoing fader at plan bar 6, after the
	// visible control has closed the incoming fader. The set must not dispatch, on a tick or a finish_now.
	const withSet = order({
		events: [...order().events, { at_bar: 6, command: { type: 'fader', deck: 1, value: 0 } }]
	});
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(withSet);
	at(19.8);
	await runner.tick();
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	at(25.5);
	await runner.tick();
	state.mixer.channels[2].fader = 0;
	const before = dispatched.length;
	at(26.5); // the timed set comes due at plan bar 6, well before the handover stop
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /outgoing deck 1 would go silent/);
	assert.ok(!dispatched.slice(before).some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the set is not dispatched');
	assert.ok(state.mixer.channels[1].fader > 0, 'the outgoing fader keeps its level');
});

test('a timed set refuses to close the outgoing path when an earlier dispatch in the same tick pauses the incoming deck', async () => {
	// The incoming fader stays open, so only the transport check sees the pause. The eq at bar 5 pauses the incoming
	// deck, then the set at bar 6 comes due in the same tick: it must not dispatch, and the outgoing deck keeps playing.
	let pause = () => {};
	const withPause = order({
		events: [
			...order().events,
			{ at_bar: 5, command: { type: 'eq', deck: 1, band: 'mid', value: 0.5 } },
			{ at_bar: 6, command: { type: 'fader', deck: 1, value: 0 } }
		]
	});
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		refuse: (command) => {
			if (command.type === 'eq') pause();
			return false;
		}
	});
	pause = () => {
		state.decks[2].playing = false;
	};
	await runner.schedule(withPause);
	at(19.8);
	await runner.tick();
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	state.mixer.channels[2].fader = 1;
	const before = dispatched.length;
	at(26.5); // plan bar 6: the eq at bar 5 and the set at bar 6 both come due in this tick
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	// The pause is seen before the bar 5 eq, which is the plan's next event, so the plan fails there rather than at the set.
	assert.match(runner.mirror().reason, /incoming deck 2 stopped mid-plan/);
	assert.ok(!dispatched.slice(before).some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the set is not dispatched');
	assert.ok(state.mixer.channels[1].fader > 0, 'the outgoing fader keeps its level');
	// Control: the same set with the incoming deck still playing dispatches.
	const control = rig({ clockBar: 19 });
	await control.runner.schedule(withPause);
	control.at(19.8);
	await control.runner.tick();
	control.state.decks[2].quantized_launch_armed = null;
	control.state.decks[2].playing = true;
	control.state.mixer.channels[2].fader = 1;
	control.at(26.5);
	await control.runner.tick();
	assert.ok(control.dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the control dispatches the set');
});

test('a timed incoming fader set that closes the room is refused while the outgoing deck is already closed', async () => {
	// The outgoing fader is closed by a visible control, so one more closed path would silence both decks. The timed
	// set that closes the incoming fader at plan bar 6 must not dispatch. Control: with the outgoing deck audible,
	// the same set dispatches.
	const withSet = order({ events: [...order().events, { at_bar: 6, command: { type: 'fader', deck: 2, value: 0 } }] });
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(withSet);
	at(19.8);
	await runner.tick();
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	state.mixer.channels[2].fader = 1;
	state.mixer.channels[1].fader = 0;
	const before = dispatched.length;
	at(26.5);
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /outgoing deck 1 would go silent: incoming deck 2 is silent/);
	assert.ok(!dispatched.slice(before).some((c) => c.type === 'fader' && c.deck === 2 && c.value === 0), 'the set is not dispatched');
	assert.ok(state.mixer.channels[2].fader > 0, 'the incoming fader keeps its level');
	const control = rig({ clockBar: 19 });
	await control.runner.schedule(withSet);
	control.at(19.8);
	await control.runner.tick();
	control.state.decks[2].quantized_launch_armed = null;
	control.state.decks[2].playing = true;
	control.state.mixer.channels[2].fader = 1;
	control.at(26.5);
	await control.runner.tick();
	assert.ok(control.dispatched.some((c) => c.type === 'fader' && c.deck === 2 && c.value === 0), 'the control dispatches the set');
});

test('the outgoing stop rechecks the incoming transport, so a pause made during an earlier dispatch fails the plan', async () => {
	// The master dispatch that precedes the handover stop pauses the incoming deck. Its fader stays open, so only
	// the transport check can see the pause: the outgoing deck must keep playing.
	let pause = () => {};
	const { runner, state, dispatched, at } = rig({
		clockBar: 19.5,
		refuse: (command) => {
			if (command.type === 'master') pause();
			return false;
		}
	});
	pause = () => {
		state.decks[2].playing = false;
	};
	await runner.schedule(order());
	at(19.8); // the quantized play at plan bar -0.25 fires here
	await runner.tick();
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	state.mixer.channels[2].fader = 1; // the incoming ramp has finished by the handover
	const before = dispatched.length;
	at(28); // plan bar 8: the master dispatch runs, then the handover stop
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /incoming deck 2 stopped mid-plan/);
	assert.ok(!dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 1 && c.playing === false), 'the outgoing stop is not dispatched');
	// Control: with the incoming deck left playing, the same handover completes.
	const control = rig({ clockBar: 19.5 });
	await control.runner.schedule(order());
	control.at(19.8);
	await control.runner.tick();
	control.state.decks[2].quantized_launch_armed = null;
	control.state.decks[2].playing = true;
	control.state.mixer.channels[2].fader = 1;
	control.at(28);
	await control.runner.tick();
	assert.ok(control.dispatched.some((c) => c.type === 'play' && c.deck === 1 && c.playing === false), 'the control stops the outgoing deck');
	assert.notEqual(control.runner.mirror().status, 'failed');
});

test('a plan mute is run-only: persist true is refused, and an unset persist is pinned to false', async () => {
	const persisted = rig();
	await assert.rejects(
		() => persisted.runner.schedule(order({ prepare: [{ type: 'master_mute', muted: true, persist: true }] })),
		/run-only, so persist must be false/
	);
	const pinned = rig();
	await pinned.runner.schedule(order({ prepare: [{ type: 'master_mute', muted: true }] }));
	assert.ok(pinned.dispatched.some((c) => c.type === 'master_mute' && c.muted === true && c.persist === false));
});

test('a muted plan whose master the operator unmutes after schedule fails before its play sounds', async () => {
	const mute = { type: 'master_mute', muted: true, persist: false };
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order({ muted: true, prepare: [mute, { type: 'seek', deck: 2, position_ms: 0 }] }));
	assert.equal(state.master.muted, true);
	state.master.muted = false; // the operator unmutes the master once schedule has returned
	at(19.8); // plan bar -0.2: the quantized play is due
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.playing === true), false);
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /master is unmuted, so the play of deck 2 would sound/);
});

test('a muted plan still plays while the master stays muted', async () => {
	const mute = { type: 'master_mute', muted: true, persist: false };
	const { runner, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order({ muted: true, prepare: [mute, { type: 'seek', deck: 2, position_ms: 0 }] }));
	at(19.8);
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'play' && c.deck === 2 && c.quantize === true), true);
	assert.notEqual(runner.mirror().status, 'failed');
});

test('finish_now on a muted plan refuses to start the armed incoming deck while the master is unmuted', async () => {
	const mute = { type: 'master_mute', muted: true, persist: false };
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order({ muted: true, prepare: [mute, { type: 'seek', deck: 2, position_ms: 0 }] }));
	at(19.8);
	await runner.tick(); // the quantized play fires and arms deck 2 for its downbeat
	const plays = dispatched.filter((c) => c.type === 'play' && c.deck === 2 && c.playing === true).length;
	assert.equal(plays, 1, 'control: the quantized play is the only play so far');
	state.master.muted = false; // the operator unmutes after the play fired
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /master is unmuted/);
	assert.equal(dispatched.filter((c) => c.type === 'play' && c.deck === 2 && c.playing === true).length, plays);
	assert.equal(runner.mirror().status, 'failed');
});

test('finish_now refuses an unfired tempo ramp the narrowed pitch range no longer allows, before any step', async () => {
	const { runner, state, dispatched } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'tempo', deck: 2, ratio: 1.05 }, to: 1.05, over_bars: 4 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } }
			]
		})
	);
	state.decks[2].pitch_range = 2; // the operator narrows deck 2 to +-2% after the ramp is registered
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /outside deck 2's pitch range/);
	assert.equal(dispatched.some((c) => c.type === 'tempo'), false, 'no tempo step may be sent');
	assert.equal(dispatched.some((c) => c.type === 'eq'), false, 'no later step may be sent either');
	assert.equal(runner.mirror().status, 'failed');
});

test('finish_now refuses an active tempo ramp the narrowed pitch range no longer allows, before any step is sent', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'tempo', deck: 2, ratio: 1.05 }, to: 1.05, over_bars: 4 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } }
			]
		})
	);
	at(20.1);
	await runner.tick(); // the tempo ramp registers while the range still allows its target
	state.decks[2].pitch_range = 2; // the operator narrows deck 2 to +-2% while the ramp is active
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /outside deck 2's pitch range/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'tempo' || c.type === 'eq'), false, 'no step may be sent');
	assert.equal(runner.mirror().status, 'failed');
});

test('finish_now rechecks the plan tracks before each remaining step, so a track replaced mid-finish sends no later step', async () => {
	const { runner, state, dispatched } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (command.type === 'play' && command.deck === 2) current.decks[1].stable_id = 'trk-replaced'; // the operator swaps the outgoing track during the play
		}
	});
	await runner.schedule(order());
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /no longer holds trk-out/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'fader' || c.type === 'eq'), false, 'no later step may be sent');
	assert.equal(runner.mirror().status, 'failed');
	assert.equal(state.decks[1].stable_id, 'trk-replaced');
});

test('finish_now rechecks ownership before a timed command step as well, not only before a ramp', async () => {
	const { runner, dispatched } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (command.type === 'eq' && command.band === 'low') current.decks[1].stable_id = 'trk-replaced'; // the operator swaps the outgoing track mid-finish
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'mid', value: 0 } }
			]
		})
	);
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /no longer holds trk-out/);
	assert.equal(dispatched.slice(before).some((c) => c.band === 'mid'), false, 'the later step must not be sent');
});

test('finish_now rechecks ownership before each active ramp it settles', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(20.1);
	await runner.tick(); // the play fires and the fader ramp at bar 0 becomes active
	state.decks[1].stable_id = 'trk-replaced'; // the operator swaps the outgoing track while the ramp is active
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /no longer holds trk-out/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'fader'), false, 'no active ramp may be settled');
});

test('finish_now rechecks ownership between two active ramps when the track is replaced mid-finish', async () => {
	let swapOnNextRamp = false;
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (swapOnNextRamp && command.type === 'fader') current.decks[1].stable_id = 'trk-replaced'; // the operator swaps the outgoing track during the first ramp
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
				{ at_bar: 0, ramp: { command: { type: 'trim', deck: 2, value: 1 }, to: 1, over_bars: 4 } }
			]
		})
	);
	at(20.1);
	await runner.tick(); // both ramps become active
	swapOnNextRamp = true;
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /no longer holds trk-out/);
	assert.equal(dispatched.slice(before).filter((c) => c.type === 'fader').length, 1, 'the first ramp settles once');
	assert.equal(dispatched.slice(before).some((c) => c.type === 'trim'), false, 'the second ramp must not be sent');
	assert.equal(state.decks[1].stable_id, 'trk-replaced');
});

test('finish_now refuses an incoming deck the operator started before the plan play, before any remaining step', async () => {
	const { runner, state, dispatched } = rig({ clockBar: 19 });
	await runner.schedule(order());
	state.decks[2].playing = true; // the operator starts the incoming deck after schedule, before the next tick
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /started before the plan's play/);
	assert.equal(dispatched.slice(before).length, 0, 'no remaining step may be sent');
	assert.equal(runner.mirror().status, 'failed');
});

test('a cancelled incoming launch leaves a track the operator replaced and armed alone', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick(); // the quantized play fires and arms deck 2
	state.decks[2].stable_id = 'trk-other'; // the operator loads another track and arms it
	state.decks[2].quantized_launch_armed = { remaining_ms: 250 };
	const before = dispatched.length;
	await runner.tick(); // the plan's track is gone, so the plan fails and cancels its own launch only
	assert.equal(runner.mirror().status, 'failed');
	assert.equal(dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 2 && c.playing === false), false, 'the operator launch must not be cancelled');
});

test('a timed tempo set is refused against a pitch range narrowed after schedule, before any step', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({ events: [{ at_bar: 0, command: { type: 'tempo', deck: 2, ratio: 1.05 } }] })
	);
	state.decks[2].pitch_range = 2; // the operator narrows deck 2 to +-2% before the set fires
	at(20.1);
	await runner.tick();
	assert.equal(dispatched.some((c) => c.type === 'tempo'), false, 'no tempo set may be sent');
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /outside deck 2's pitch range/);
});

test('finish_now refuses its own play when the operator starts the incoming deck during an earlier finish step', async () => {
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (command.type === 'fader' && command.deck === 1) current.decks[2].playing = true; // the operator starts deck 2 during the ramp
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 1, value: 0.5 }, to: 0.5, over_bars: 4 } },
				{ at_bar: 4, command: { type: 'play', deck: 2, playing: true, quantize: true } }
			]
		})
	);
	at(20.1);
	await runner.tick(); // the outgoing fader ramp becomes active
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /started before the plan's play/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 2), false, 'the plan play must not be sent');
});

test('finish_now rechecks an early incoming start before each active ramp it settles', async () => {
	let startOnNextRamp = false;
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (startOnNextRamp && command.type === 'fader' && command.deck === 1) current.decks[2].playing = true; // the operator starts deck 2 during the first ramp
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 1, value: 0.5 }, to: 0.5, over_bars: 4 } },
				{ at_bar: 0, ramp: { command: { type: 'trim', deck: 2, value: 1 }, to: 1, over_bars: 4 } }
			]
		})
	);
	at(20.1);
	await runner.tick(); // both ramps become active while the plan's play is still unfired
	startOnNextRamp = true;
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /started before the plan's play/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'trim'), false, 'the second ramp must not be sent');
	assert.equal(state.decks[2].playing, true);
});

test('finish_now rechecks an early incoming start before each timed command it sends', async () => {
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (command.type === 'eq' && command.band === 'low') current.decks[2].playing = true; // the operator starts deck 2 during the eq
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
				{ at_bar: 4, command: { type: 'play', deck: 2, playing: true, quantize: true } }
			]
		})
	);
	const before = dispatched.length; // the eq is still unfired, so finish_now sends it, and the operator's start lands during it
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /started before the plan's play/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'play' && c.deck === 2), false, 'the plan play must not be sent');
	assert.equal(state.decks[2].playing, true);
});

test('the tick ramp pass re-reads plan ownership before each ramp it sends, so a track replaced mid-pass sends no later ramp', async () => {
	let swapOnFader = false;
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (swapOnFader && command.type === 'fader') current.decks[1].stable_id = 'trk-replaced'; // the operator swaps the outgoing track during the first active ramp
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 1, value: 0.5 }, to: 0.5, over_bars: 4 } },
				{ at_bar: 0, ramp: { command: { type: 'trim', deck: 2, value: 1 }, to: 1, over_bars: 4 } }
			]
		})
	);
	at(20.1);
	await runner.tick(); // both ramps register; the second is armed by this same pass
	swapOnFader = true;
	const before = dispatched.length;
	at(20.2);
	await runner.tick(); // both ramps are active in this pass, so the first one's dispatch can swap the track under the second
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /no longer holds trk-out/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'trim'), false, 'the second ramp must not be sent');
	assert.equal(state.decks[1].stable_id, 'trk-replaced');
});


test('the operator electing a third deck master mid-plan fails the plan before its next tick step', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order({ events: [{ at_bar: 0, command: { type: 'fader', deck: 1, value: 0.8 } }] }));
	state.master_deck = 3; // a visible control re-elects the master after schedule; the clock deck stays put
	at(20.1);
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /master moved to deck 3 off clock deck 1/);
	assert.equal(dispatched.some((c) => c.type === 'fader'), false, 'no fader step may be sent under another master');
});

test('finish_now refuses to send its steps once the operator elected another master', async () => {
	const { runner, state, dispatched } = rig({ clockBar: 19 });
	await runner.schedule(order());
	state.master_deck = 3;
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /master moved to deck 3 off clock deck 1/);
	assert.equal(dispatched.slice(before).length, 0, 'no finish step may be sent under another master');
	assert.equal(runner.mirror().status, 'failed');
});

test('a ramp pass rechecks the incoming transport before each step, so a pause during an earlier ramp sends no later ramp', async () => {
	let pauseOnOutgoingFader = false;
	const { runner, state, dispatched, at } = rig({
		clockBar: 19.5,
		onDispatch: (command, current) => {
			if (pauseOnOutgoingFader && command.type === 'fader' && command.deck === 1) current.decks[2].playing = false; // the operator pauses the incoming deck during the first active ramp
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 1, value: 0.5 }, to: 0.5, over_bars: 4 } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick(); // the quantized play fires at plan bar -0.25
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	at(20.1);
	await runner.tick(); // both ramps register
	pauseOnOutgoingFader = true;
	const before = dispatched.length;
	at(20.2);
	await runner.tick(); // both ramps are active: the first one's dispatch pauses the incoming deck before the second is sent
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /incoming deck 2 stopped mid-plan/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'fader' && c.deck === 2), false, 'the incoming ramp must not be sent');
});

test('finish_now rechecks ownership before each attempt to start an armed launch, so a track replaced by the first play is not played', async () => {
	let swapOnPlay = false;
	const { runner, state, dispatched, at } = rig({
		clockBar: 19.5,
		onDispatch: (command, current) => {
			if (swapOnPlay && command.type === 'play' && command.deck === 2) current.decks[2].stable_id = 'trk-replaced'; // the operator loads another track during the first play
		}
	});
	await runner.schedule(order());
	at(19.8);
	await runner.tick(); // the quantized play fires at plan bar -0.25 and arms the incoming deck
	assert.ok(state.decks[2].quantized_launch_armed, 'the launch is armed, not playing');
	swapOnPlay = true;
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /no longer holds trk-in/);
	assert.equal(dispatched.slice(before).filter((c) => c.type === 'play' && c.deck === 2).length, 1, 'the second attempt must not play the replacement');
});

test('finish_now rechecks the incoming transport before each remaining ramp, so a pause during an earlier finish step sends no later step', async () => {
	let pauseOnOutgoingFader = false;
	const { runner, state, dispatched, at } = rig({
		clockBar: 19.5,
		onDispatch: (command, current) => {
			if (pauseOnOutgoingFader && command.type === 'fader' && command.deck === 1) current.decks[2].playing = false; // the operator pauses the incoming deck during the first finish step
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 1, value: 0.5 }, to: 0.5, over_bars: 4 } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick(); // the quantized play fires at plan bar -0.25
	state.decks[2].quantized_launch_armed = null;
	state.decks[2].playing = true;
	at(20.1);
	await runner.tick(); // both ramps register
	pauseOnOutgoingFader = true;
	const before = dispatched.length;
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }), /incoming deck 2 stopped mid-plan/);
	assert.equal(dispatched.slice(before).some((c) => c.type === 'fader' && c.deck === 2), false, 'the incoming ramp must not be sent');
});

test('after the handover master event, an operator who elects a third deck fails the plan before its next event', async () => {
	// Codex (PR #5822, BLOCKING): the handover's master dispatch is awaited, and an election made during it must not pass as the plan's own.
	const { runner, state, dispatched, at } = rig({
		clockBar: 19,
		onDispatch: (command, current) => {
			if (command.type === 'master') current.master_deck = 3; // the operator elects deck 3 while the handover's master dispatch is awaited
		}
	});
	await runner.schedule(
		order({
			events: [
				{ at_bar: 3, command: { type: 'master', deck: 2 } },
				{ at_bar: 5, command: { type: 'fader', deck: 1, value: 0 } }
			]
		})
	);
	at(22.5);
	await runner.tick();
	at(30);
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /master moved to deck 3 off incoming deck 2 after the handover/);
	assert.ok(!dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the later event must not run');
	assert.equal(state.master_deck, 3);
});

test('a pause on the incoming deck during the final awaited dispatch fails the plan rather than marking it done', async () => {
	for (const [paused, expected] of [
		[true, 'failed'],
		[false, 'done'] // control: the same final stop with the incoming deck still playing
	]) {
		const { runner, state, at } = rig({
			clockBar: 19,
			onDispatch: (command, current) => {
				if (paused && command.type === 'play' && command.deck === 1 && command.playing === false) current.decks[2].playing = false; // the operator pauses the incoming deck during the handover stop
			}
		});
		await runner.schedule(order());
		at(19.8); // the quantized play fires and arms
		await runner.tick();
		state.decks[2].quantized_launch_armed = null; // the launch lands
		state.decks[2].playing = true;
		at(20.2);
		await runner.tick();
		at(28.2); // plan bar 8.2: the master and the handover stop fire on this tick
		await runner.tick();
		assert.equal(runner.mirror().status, expected);
		if (paused) assert.match(runner.mirror().reason, /incoming deck 2 stopped mid-plan/);
	}
});

test('finish_now rechecks the incoming transport after its final awaited dispatch, before it marks the plan done', async () => {
	for (const [paused, expected] of [
		[true, 'failed'],
		[false, 'done'] // control: the same finish with the incoming deck still playing
	]) {
		const { runner, state, at } = rig({
			clockBar: 19.5,
			onDispatch: (command, current) => {
				if (paused && command.type === 'play' && command.deck === 1 && command.playing === false) current.decks[2].playing = false; // the operator pauses the incoming deck during the finish's handover stop
			}
		});
		await runner.schedule(order());
		at(19.8);
		await runner.tick();
		state.decks[2].quantized_launch_armed = null;
		state.decks[2].playing = true;
		at(20.2);
		await runner.tick();
		const finished = runner.amend({ id: 'plan-test', action: 'finish_now' });
		if (paused) await assert.rejects(finished, /incoming deck 2 stopped mid-plan/);
		else await finished;
		assert.equal(runner.mirror().status, expected);
	}
});

test('AGENT-30: a step set inside the lookahead is dispatched early with its bar\'s AudioContext time', async () => {
	const { runner, state, dispatched, at, advance } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8); // the launch fires one beat early and arms
	await runner.tick();
	at(20); // it lands on the downbeat
	await runner.tick();
	const eqEvents = () => dispatched.filter((c) => c.type === 'eq');
	// One tick earlier, 150 ms out, the step is not due: nothing for it is sent.
	state.decks[1].position_ms = 23 * BAR_MS - 150;
	await runner.tick();
	assert.equal(eqEvents().length, 0);
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	assert.equal(eqEvents().length, 1);
	assert.ok(Math.abs(eqEvents()[0].at_context_sec - 1000.05) < 1e-9, `at_context_sec ${eqEvents()[0].at_context_sec}`);
	// It fires once: the next tick, past its bar, sends nothing more for it (the audio clock moves with the playhead).
	advance(60);
	state.decks[1].position_ms = 23 * BAR_MS + 10;
	await runner.tick();
	assert.equal(eqEvents().length, 1);
});

test('AGENT-30: a step at double tempo is timed on real seconds, and a ramp is never scheduled', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].pitch = 2; // 100 track ms ahead is 50 real ms
	state.decks[1].position_ms = 23 * BAR_MS - 100;
	await runner.tick();
	const eq = dispatched.find((c) => c.type === 'eq');
	assert.ok(Math.abs(eq.at_context_sec - 1000.05) < 1e-9);
	assert.ok(dispatched.filter((c) => c.type === 'fader').every((c) => c.at_context_sec === undefined), 'ramp steps run on the tick');
});

test('AGENT-30: an outgoing cut scheduled at the armed launch time is not held back for the launch', async () => {
	const { runner, state, dispatched } = rig({ clockBar: 19, landsDownbeat: false });
	await runner.schedule(
		order({
			bars: 1,
			style: 'cut',
			prepare: [{ type: 'seek', deck: 2, position_ms: 0 }, { type: 'fader', deck: 2, value: 1 }],
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 1, command: { type: 'master', deck: 2 } },
				{ at_bar: 1, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	state.decks[1].position_ms = 19 * BAR_MS - 500; // one beat early: the launch arms
	await runner.tick();
	state.decks[2].quantized_launch_armed = { remaining_ms: 40, launch_at_context_sec: 1000.04 };
	state.decks[1].position_ms = 19 * BAR_MS - 40;
	await runner.tick();
	const cut = dispatched.find((c) => c.type === 'fader' && c.deck === 1);
	assert.ok(cut, 'the cut is sent before its bar, armed with the launch');
	assert.ok(Math.abs(cut.at_context_sec - 1000.04) < 1e-9);
});

test('AGENT-30 control: an outgoing close scheduled BEFORE the armed launch still waits for it', async () => {
	const { runner, state, dispatched } = rig({ clockBar: 19, landsDownbeat: false });
	await runner.schedule(
		order({
			bars: 1,
			style: 'cut',
			prepare: [{ type: 'seek', deck: 2, position_ms: 0 }, { type: 'fader', deck: 2, value: 1 }],
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 1, command: { type: 'master', deck: 2 } },
				{ at_bar: 1, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	state.decks[1].position_ms = 19 * BAR_MS - 500;
	await runner.tick();
	state.decks[2].quantized_launch_armed = { remaining_ms: 400, launch_at_context_sec: 1000.4 };
	state.decks[1].position_ms = 19 * BAR_MS - 40;
	await runner.tick();
	assert.equal(dispatched.find((c) => c.type === 'fader' && c.deck === 1), undefined);
});

test('msAtBar inverts barAt', () => {
	const downbeats = plan.downbeatsMs(GRID);
	for (const b of [0.5, 1, 4.5, 30.25, 70]) assert.ok(Math.abs(plan.barAt(downbeats, plan.msAtBar(downbeats, b)) - b) < 1e-9);
});

test('AGENT-30: a scheduled step is timed from ONE engine observation, not a stale playhead plus a later clock', async () => {
	const { runner, state, dispatched, at, advance, pinAnchor } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	// The engine presented the playhead 50 ms before bar 24 at context 1000.000; the live clock has since moved 30 ms.
	pinAnchor({ position_ms: 23 * BAR_MS - 50, context_sec: 1000, pitch: 1 });
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	advance(30);
	await runner.tick();
	const eq = dispatched.find((c) => c.type === 'eq');
	assert.ok(Math.abs(eq.at_context_sec - 1000.05) < 1e-9, `stamped ${eq.at_context_sec}, not 1000.08`);
});

test('AGENT-30: an aborted plan revokes a step still waiting for its audio-clock time', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.mixer.channels[1].eq_low = 0.5;
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	assert.equal(dispatched.filter((c) => c.type === 'eq').length, 1, 'the kill is sent ahead');
	await runner.amend({ id: 'plan-test', action: 'abort' });
	const eqs = dispatched.filter((c) => c.type === 'eq');
	assert.deepEqual(eqs.at(-1), { type: 'eq', deck: 1, band: 'low', value: 0.5 }, 'a plain set of the old value revokes it');
});

test('AGENT-30 control: a step that already landed is not revoked when the plan ends', async () => {
	const { runner, state, dispatched, at, advance } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.mixer.channels[1].eq_low = 0.5;
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	advance(200); // past its time
	await runner.amend({ id: 'plan-test', action: 'abort' });
	assert.equal(dispatched.filter((c) => c.type === 'eq').length, 1);
});

test('AGENT-30: a step on a control with a running ramp waits for its bar, so the ramp runs until it lands', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 4 } },
				{ at_bar: 2, command: { type: 'fader', deck: 2, value: 0.9 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 21 * BAR_MS - 50; // 50 ms before plan bar 2
	await runner.tick();
	assert.ok(dispatched.filter((c) => c.type === 'fader' && c.value === 0.9).length === 0, 'not sent ahead mid-ramp');
	state.decks[1].position_ms = 21 * BAR_MS;
	await runner.tick();
	const step = dispatched.find((c) => c.type === 'fader' && c.value === 0.9);
	assert.ok(step && step.at_context_sec === undefined, 'fires on its bar, after the ramp settles to it');
});

/** Two steps on deck 1's EQ (low at bar 4, then `band` at 4.01), both inside the lookahead of one tick when the bands
 *  differ. On one band the second is held until the first lands (Codex P2 #7594). */
async function twoPendingSteps(rigOptions = {}, band = 'high') {
	const r = rig({ clockBar: 19, ...rigOptions });
	r.state.mixer.channels[1].eq_high = 0.5;
	await r.runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 2 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0.2 } },
				{ at_bar: 4.01, command: { type: 'eq', deck: 1, band, value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	r.at(19.8);
	await r.runner.tick();
	r.at(20);
	await r.runner.tick();
	r.state.decks[1].position_ms = 23 * BAR_MS - 50;
	await r.runner.tick();
	return r;
}

test('AGENT-30: an abort revokes pending steps newest first, back to the value before each', async () => {
	const { runner, dispatched, state } = await twoPendingSteps();
	assert.equal(dispatched.filter((c) => c.type === 'eq' && c.at_context_sec !== undefined).length, 2);
	await runner.amend({ id: 'plan-test', action: 'abort' });
	const plain = dispatched.filter((c) => c.type === 'eq' && c.at_context_sec === undefined);
	assert.deepEqual(plain.map((c) => [c.band, c.value]), [['high', 0.5], ['low', 0.5]], 'newest first, each back to its original');
	assert.equal(state.mixer.channels[1].eq_low, 0.5);
});

test('AGENT-30: one failed revoke does not abandon the others, and the failure is reported', async () => {
	let refusals = 1;
	const { runner, dispatched } = await twoPendingSteps({
		refuse: (command) => command.type === 'eq' && command.at_context_sec === undefined && refusals-- > 0
	});
	await runner.amend({ id: 'plan-test', action: 'abort' });
	assert.equal(dispatched.filter((c) => c.type === 'eq' && c.at_context_sec === undefined).length, 2, 'both attempted');
	assert.match(runner.mirror().reason, /could not be settled/);
});

test('AGENT-30: finish_now lands a step still waiting for its audio-clock time at once', async () => {
	const { runner, dispatched } = await twoPendingSteps();
	await runner.amend({ id: 'plan-test', action: 'finish_now' });
	const plain = dispatched.filter((c) => c.type === 'eq' && c.at_context_sec === undefined);
	assert.deepEqual(plain.slice(0, 2).map((c) => c.value), [0.2, 0], 'applied in plan order, the later step last');
});

test('AGENT-30: a step is not sent ahead while the clock deck tempo is moving', async () => {
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 2 } },
				{ at_bar: 2, ramp: { command: { type: 'tempo', deck: 1, ratio: 1.02 }, to: 1.02, over_bars: 4 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	at(22);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	assert.equal(dispatched.filter((c) => c.type === 'eq').length, 0, 'not sent ahead mid-glide');
	state.decks[1].position_ms = 23 * BAR_MS;
	await runner.tick();
	const eq = dispatched.find((c) => c.type === 'eq');
	assert.ok(eq && eq.at_context_sec === undefined, 'fires on its bar');
});

test('AGENT-30: a step an operator already overrode is neither revoked nor reapplied', async () => {
	for (const action of ['abort', 'finish_now']) {
		const { runner, dispatched, state, plainSet } = await twoPendingSteps();
		plainSet({ type: 'eq', deck: 1, band: 'low', value: 0.8 }); // the operator's plain sets cancel both scheduled steps
		plainSet({ type: 'eq', deck: 1, band: 'high', value: 0.8 });
		const before = dispatched.length;
		await runner.amend({ id: 'plan-test', action });
		assert.equal(dispatched.slice(before).filter((c) => c.type === 'eq' && c.deck === 1).length, 0, action);
		assert.equal(state.mixer.channels[1].eq_low, 0.8, action);
	}
});

test('AGENT-30: with no valid anchor (the timeline moved), a step fires on its bar instead of being sent ahead', async () => {
	const { runner, state, dispatched, at, pinAnchor } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	pinAnchor(null);
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	assert.equal(dispatched.filter((c) => c.type === 'eq').length, 0);
	assert.equal(runner.mirror().status, 'running', 'no anchor is not a failure');
	state.decks[1].position_ms = 23 * BAR_MS;
	await runner.tick();
	const eq = dispatched.find((c) => c.type === 'eq');
	assert.ok(eq && eq.at_context_sec === undefined);
});

test('AGENT-30: a step is timed with the anchor\'s own pitch, not a newer deck read', async () => {
	const { runner, state, dispatched, at, pinAnchor } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	pinAnchor({ position_ms: 23 * BAR_MS - 100, context_sec: 1000, pitch: 2 });
	state.decks[1].pitch = 1; // the mirror has a different (later) pitch
	state.decks[1].position_ms = 23 * BAR_MS - 100;
	await runner.tick();
	const eq = dispatched.find((c) => c.type === 'eq');
	assert.ok(Math.abs(eq.at_context_sec - 1000.05) < 1e-9, `stamped ${eq.at_context_sec}`);
});

test('AGENT-30: a finished plan\'s steps are never revoked by the next plan\'s abort', async () => {
	const { runner, state, dispatched, at, advance } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			bars: 4,
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 1 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
				{ at_bar: 4, command: { type: 'master', deck: 2 } },
				{ at_bar: 4, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(21);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50; // the kill is sent ahead, its time still to come
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS;
	advance(100); // the kill has landed: only now is the plan done (Codex #7594)
	await runner.tick();
	assert.equal(runner.mirror().status, 'done');
	// A new plan, aborted at once: it must not revoke the finished plan's kill.
	Object.assign(state.decks[1], { playing: true, position_ms: 30 * BAR_MS });
	Object.assign(state.decks[2], { playing: false, quantized_launch_armed: null });
	state.master_deck = 1;
	const before = dispatched.length;
	await runner.schedule(order({ id: 'plan-two', start_bar: 40 }));
	await runner.amend({ id: 'plan-two', action: 'abort' });
	assert.equal(dispatched.slice(before).filter((c) => c.type === 'eq').length, 0);
});

test('AGENT-30: a ramp whose end has passed counts as settled, so a step after it is still sent ahead', async () => {
	for (const control of ['same-control', 'tempo']) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		const ramp =
			control === 'same-control'
				? { at_bar: 0, ramp: { command: { type: 'eq', deck: 1, band: 'low', value: 0.4 }, to: 0.4, over_bars: 3.9 } }
				: { at_bar: 0, ramp: { command: { type: 'tempo', deck: 1, ratio: 1.01 }, to: 1.01, over_bars: 3.9 } };
		await runner.schedule(
			order({
				events: [
					{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
					{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 2 } },
					ramp,
					{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
					{ at_bar: 8, command: { type: 'master', deck: 2 } },
					{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
				]
			})
		);
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		at(23); // the ramp is still running
		await runner.tick();
		// The previous tick saw the ramp running; this one lands after its end (bar 3.9) and 50 ms before the step.
		state.decks[1].position_ms = 23 * BAR_MS - 50;
		await runner.tick();
		const step = dispatched.find((c) => c.type === 'eq' && c.value === 0);
		assert.ok(step && typeof step.at_context_sec === 'number', `${control}: sent ahead once its ramp has ended`);
	}
});

test('finish_now and a timer tick never run at once: a tick during finish_now does nothing', async () => {
	// Codex P1 on #5822: while finish_now awaits a dispatch, the 16 ms timer entered _advance(). Here the first of
	// finish_now's two presses disarms the launch and is held in flight, so the incoming deck reads neither playing
	// nor armed; a concurrent tick then saw "stopped mid-plan" and sent a stop before the handover overwrote it.
	let release;
	const held = new Promise((resolve) => (release = resolve));
	let holding = true;
	const { runner, dispatched, at } = rig({
		clockBar: 19,
		landsDownbeat: false,
		hold: (command) => (holding && command.type === 'play' && command.deck === 2 && command.playing && !command.quantize ? held : null)
	});
	await runner.schedule(order());
	at(19.8);
	await runner.tick(); // the quantized launch fires and arms
	const finishing = runner.amend({ id: 'plan-test', action: 'finish_now' });
	await new Promise((resolve) => setTimeout(resolve, 0)); // finish_now is now awaiting the held press
	await runner.tick(); // the timer tick that raced it
	holding = false;
	release();
	await finishing;
	assert.equal(runner.mirror().status, 'done', runner.mirror().reason ?? '');
	assert.equal(dispatched.filter((c) => c.type === 'play' && c.deck === 2 && c.playing === false).length, 0, 'no stop of the incoming deck');
});

test('AGENT-30: a step is not sent ahead while an unfired ramp on its control starts at or before its bar', async () => {
	// Sol P1 on #7594: the ramp is authored but has not fired yet (it shares the step's bar and comes first).
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 2 } },
				{ at_bar: 4, ramp: { command: { type: 'eq', deck: 1, band: 'low', value: 0.3 }, to: 0.3, over_bars: 2 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	assert.equal(dispatched.filter((c) => c.type === 'eq' && c.value === 0).length, 0, 'held behind the unfired ramp');
});

test('the engine automatically promoting the incoming deck once the outgoing path closes is the handover, not a lost master', async () => {
	for (const [electedDeck, expected] of [
		[2, 'done'], // the audible incoming deck takes master on its own when the outgoing fader closes
		[3, 'failed'] // control: a third deck elected in the same place is still refused
	]) {
		const { runner, state, at } = rig({
			clockBar: 19,
			onDispatch: (command, current) => {
				if (command.type === 'fader' && command.deck === 1 && command.value === 0) current.master_deck = electedDeck; // the engine's level handoff elects the deck that is now audible
			}
		});
		await runner.schedule(
			order({
				events: [
					{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
					{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 1 } },
					{ at_bar: 1, command: { type: 'fader', deck: 1, value: 0 } },
					{ at_bar: 8, command: { type: 'master', deck: 2 } },
					{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
				]
			})
		);
		at(19.8);
		await runner.tick();
		state.decks[2].quantized_launch_armed = null;
		state.decks[2].playing = true;
		at(20.2);
		await runner.tick();
		at(21.2); // plan bar 1.2: the outgoing fader closes and the engine elects
		await runner.tick();
		at(28.2); // plan bar 8.2: the master and the handover stop fire
		await runner.tick();
		assert.equal(runner.mirror().status, expected, runner.mirror().reason ?? '');
	}
});

test('AGENT-30: a step is not sent ahead while an earlier event in plan order has not fired', async () => {
	// Claude P1 on #7594: a crossfader opening at bar 3.98 must land before a cut at bar 4 is judged.
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 2 } },
				{ at_bar: 3.98, command: { type: 'crossfader', value: 0.5 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50; // bar 3.975: the crossfader event is not due yet
	await runner.tick();
	assert.equal(dispatched.filter((c) => c.type === 'eq').length, 0, 'held behind the unfired crossfader');
	state.decks[1].position_ms = 23 * BAR_MS - 30; // bar 3.985: the crossfader fires, then the cut may go ahead
	await runner.tick();
	const eq = dispatched.find((c) => c.type === 'eq');
	assert.ok(dispatched.findIndex((c) => c.type === 'crossfader') < dispatched.indexOf(eq), 'crossfader first');
	assert.equal(typeof eq.at_context_sec, 'number');
});

test('AGENT-30: a second step on a control is not sent ahead while the first is still waiting to land', async () => {
	// Codex P2/BLOCKING on #7594: both would seed their ramps from the value before either lands.
	const sameBand = await twoPendingSteps({}, 'low');
	const ahead = sameBand.dispatched.filter((c) => c.type === 'eq' && typeof c.at_context_sec === 'number');
	assert.equal(ahead.length, 1, 'only the first is scheduled ahead');
	// Control: on another band the second is scheduled ahead too.
	const otherBand = await twoPendingSteps();
	assert.equal(otherBand.dispatched.filter((c) => c.type === 'eq' && typeof c.at_context_sec === 'number').length, 2);
});

test('AGENT-30: a plan whose last step is still waiting on the audio clock is not done yet', async () => {
	// Codex P2/BLOCKING on #7594: marking it done let dispose skip the revoke and a next plan start under it.
	const { runner, state, dispatched, at, advance } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			bars: 4,
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 1 } },
				{ at_bar: 3, command: { type: 'master', deck: 2 } },
				{ at_bar: 3, command: { type: 'play', deck: 1, playing: false } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(21);
	await runner.tick();
	at(23);
	await runner.tick();
	state.decks[1].playing = true; // keep the clock running to the last bar
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	assert.equal(typeof dispatched.find((c) => c.type === 'eq')?.at_context_sec, 'number', 'sent ahead');
	assert.equal(runner.mirror().status, 'running', 'not done while its step waits');
	advance(100);
	state.decks[1].position_ms = 23 * BAR_MS + 60;
	await runner.tick();
	assert.equal(runner.mirror().status, 'done');
});

test('AGENT-30: an extend revokes a step sent ahead and schedules it again at its new bar', async () => {
	// Sol P1 on #7594: it was marked fired and stayed armed at its old bar while the rest moved later.
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(order());
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50; // the bar-4 kill goes ahead
	await runner.tick();
	assert.equal(dispatched.filter((c) => c.type === 'eq' && typeof c.at_context_sec === 'number').length, 1);
	await runner.amend({ id: 'plan-test', action: 'extend', bars: 4 });
	const plain = dispatched.filter((c) => c.type === 'eq' && c.at_context_sec === undefined);
	assert.deepEqual(plain.at(-1), { type: 'eq', deck: 1, band: 'low', value: 0.5 }, 'revoked to the value it replaced');
	state.decks[1].position_ms = 27 * BAR_MS - 50; // 50 ms before its new bar 8
	await runner.tick();
	assert.equal(dispatched.filter((c) => c.type === 'eq' && typeof c.at_context_sec === 'number').length, 2, 'sent again at bar 8');
});

test('AGENT-30: a close at the bar of a pending open is judged against the room once the open lands', async () => {
	// Codex BLOCKING on #7594: the open is sent ahead but mirrored only when it lands, so the close read silence.
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 4, command: { type: 'fader', deck: 2, value: 1 } },
				{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	state.mixer.channels[2].fader = 0;
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50; // bar 3.975: both steps inside the lookahead
	await runner.tick();
	const faders = dispatched.filter((c) => c.type === 'fader');
	assert.deepEqual(faders.map((c) => [c.deck, c.value]), [[2, 1], [1, 0]], 'the open, then the close');
	assert.notEqual(runner.mirror().status, 'failed', runner.mirror().reason);
	// Control: with no open before it the same close still fails the plan as silent.
	const bare = rig({ clockBar: 19 });
	await bare.runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	bare.state.mixer.channels[2].fader = 0;
	bare.at(19.8);
	await bare.runner.tick();
	bare.at(20);
	await bare.runner.tick();
	bare.state.decks[1].position_ms = 23 * BAR_MS - 50;
	await bare.runner.tick();
	assert.notEqual(bare.runner.mirror().status, 'failed', 'not sent ahead into an unsafe room (Codex #7594): it waits for its bar');
	bare.state.decks[1].position_ms = 23 * BAR_MS; // its bar: judged there, it fails as before
	await bare.runner.tick();
	assert.equal(bare.runner.mirror().status, 'failed');
});

for (const [cancel, rejudged] of [['the opening', true], ['an unrelated eq step', false]]) {
	test(`AGENT-30 (Codex #7594): an operator cancelling ${cancel} ${rejudged ? 'sends the pending cut back to be judged on its bar' : 'leaves the pending cut armed'}`, async () => {
		const { runner, state, dispatched, at, plainSet, stepStates } = rig({ clockBar: 19 });
		await runner.schedule(
			order({
				events: [
					{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
					{ at_bar: 4, command: { type: 'eq', deck: 2, band: 'high', value: 0.3 } },
					{ at_bar: 4, command: { type: 'fader', deck: 2, value: 1 } },
					{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
					{ at_bar: 8, command: { type: 'master', deck: 2 } },
					{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
				]
			})
		);
		state.mixer.channels[2].fader = 0;
		state.mixer.channels[2].eq_high = 0.5;
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		state.decks[1].position_ms = 23 * BAR_MS - 50; // all three steps go ahead
		await runner.tick();
		const cut = dispatched.find((c) => c.type === 'fader' && c.deck === 1 && c.step_id);
		assert.ok(cut, 'the cut went ahead, judged against the pending opening');
		plainSet(rejudged ? { type: 'fader', deck: 2, value: 0 } : { type: 'eq', deck: 2, band: 'high', value: 0.5 }); // the operator
		await runner.tick();
		if (rejudged) {
			assert.notEqual(stepStates.get(cut.step_id), 'waiting', 'the cut is no longer armed against a cancelled opening');
			state.decks[1].position_ms = 23 * BAR_MS; // its bar
			await runner.tick();
			assert.equal(runner.mirror().status, 'failed', 'judged again it would silence the room, as on its bar');
		} else {
			assert.equal(stepStates.get(cut.step_id), 'waiting', 'control: an unrelated cancel leaves the cut as judged');
			assert.notEqual(runner.mirror().status, 'failed', runner.mirror().reason);
		}
	});
}

test('AGENT-30 (Codex #7594): finish_now after an operator cancelled the opening re-judges the pending cut, never applies it blind', async () => {
	const { runner, state, at, plainSet } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 4, command: { type: 'fader', deck: 2, value: 1 } },
				{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	state.mixer.channels[2].fader = 0;
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50; // the opening and the cut go ahead
	await runner.tick();
	plainSet({ type: 'fader', deck: 2, value: 0 }); // the operator cancels the opening; no tick runs before finish_now
	await runner.amend({ id: 'plan-test', action: 'finish_now' }).catch(() => undefined);
	assert.notEqual(runner.mirror().status, 'done');
	assert.ok(state.mixer.channels[1].fader > 0 || state.mixer.channels[2].fader > 0, 'the room is never left with both paths shut');
});

for (const [closeTrim, rejudged] of [[true, true], [false, false]]) {
	test(`AGENT-30 (Codex #7594): ${closeTrim ? 'an operator closing the incoming trim sends a pending cut back to its bar' : 'control: an untouched room keeps the pending cut armed'}`, async () => {
		const { runner, state, dispatched, at, stepStates } = rig({ clockBar: 19 });
		await runner.schedule(
			order({
				events: [
					{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
					{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 2 } },
					{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
					{ at_bar: 8, command: { type: 'master', deck: 2 } },
					{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
				]
			})
		);
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		state.decks[1].position_ms = 22 * BAR_MS; // the opening ramp has finished
		await runner.tick();
		state.decks[1].position_ms = 23 * BAR_MS - 50; // the cut goes ahead
		await runner.tick();
		const cut = dispatched.find((c) => c.type === 'fader' && c.deck === 1 && c.step_id);
		assert.ok(cut, 'the cut went ahead');
		if (closeTrim) state.mixer.channels[2].trim = 0; // the operator shuts the incoming path
		await runner.tick();
		assert.equal(stepStates.get(cut.step_id) === 'waiting', !rejudged, rejudged ? 'revoked: it would now silence the room' : 'still armed');
	});
}

test('AGENT-30 (Codex #7594): an opening ramp that finishes before the cut is taken at its target when the cut is judged ahead', async () => {
	// At 120 BPM a fader ramp over bars 3.96 to 3.98 opens the incoming path before an outgoing cut at bar 4. Judged on
	// the tick at 3.965 with the ramp's current value, the cut read as silencing the room and failed the plan.
	const { runner, state, dispatched, at } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 3.96, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 0.02 } },
				{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	state.mixer.channels[2].fader = 0;
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 22 * BAR_MS + 0.96 * BAR_MS; // plan bar 3.96: the ramp starts, its fader still shut
	await runner.tick();
	assert.notEqual(runner.mirror().status, 'failed', runner.mirror().reason);
	// Codex #7594 (next round): the ramp advances only on page ticks, so the cut is not sent ahead while the incoming
	// fader is still shut; it waits for its bar.
	assert.equal(dispatched.find((c) => c.type === 'fader' && c.deck === 1), undefined, 'not sent ahead of an unopened room');
	state.decks[1].position_ms = 23 * BAR_MS; // bar 4: the ramp has run out, the cut fires on its bar
	await runner.tick();
	assert.notEqual(runner.mirror().status, 'failed', runner.mirror().reason);
	assert.ok(dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the cut went out on its bar');
});

async function armedLaunchWithPendingCut(options = {}) {
	const r = rig({ clockBar: 19, landsDownbeat: false, ...options });
	await r.runner.schedule(
		order({
			bars: 1,
			style: 'cut',
			prepare: [{ type: 'seek', deck: 2, position_ms: 0 }, { type: 'fader', deck: 2, value: 1 }],
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 1, command: { type: 'master', deck: 2 } },
				{ at_bar: 1, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	r.state.decks[1].position_ms = 19 * BAR_MS - 500;
	await r.runner.tick();
	r.state.decks[2].quantized_launch_armed = { remaining_ms: 40, launch_at_context_sec: 1000.04 };
	r.state.decks[1].position_ms = 19 * BAR_MS - 40;
	await r.runner.tick();
	assert.equal(typeof r.dispatched.find((c) => c.type === 'fader' && c.deck === 1)?.at_context_sec, 'number', 'the cut is pending');
	return r;
}

const plainCut = (c) => c.type === 'fader' && c.deck === 1 && c.value === 0 && c.at_context_sec === undefined;
const unquantizedPlay = (c) => c.type === 'play' && c.deck === 2 && c.playing === true && c.quantize !== true;

test('AGENT-30 ordering: finish_now starts the armed incoming deck before it lands a pending outgoing cut', async () => {
	// Codex BLOCKING on #7594: the cut was applied first, so a refused launch left the room silent.
	const { runner, dispatched } = await armedLaunchWithPendingCut();
	await runner.amend({ id: 'plan-test', action: 'finish_now' });
	const play = dispatched.findIndex(unquantizedPlay);
	assert.ok(play >= 0, 'the incoming deck is started');
	const cut = dispatched.findIndex(plainCut);
	assert.ok(cut === -1 || cut > play, `the cut lands after the start (play ${play}, cut ${cut})`);
});

test('AGENT-30 ordering: a finish_now whose launch is refused revokes the pending cut, so the outgoing stays open', async () => {
	const { runner, state, dispatched } = await armedLaunchWithPendingCut({ refuse: (c) => unquantizedPlay(c) });
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }));
	assert.equal(runner.mirror().status, 'failed');
	assert.equal(dispatched.findIndex(plainCut), -1, 'the cut never lands');
	assert.equal(state.mixer.channels[1].fader, 1);
});

test('AGENT-30 ordering: dispose revokes a step still waiting for its audio-clock time', async () => {
	const { runner, state, dispatched } = await armedLaunchWithPendingCut();
	await runner.dispose();
	assert.deepEqual(dispatched.filter((c) => c.type === 'fader' && c.deck === 1).at(-1), { type: 'fader', deck: 1, value: 1 });
	assert.equal(state.mixer.channels[1].fader, 1);
});

test('AGENT-30 ordering: a launch the operator disarms fails the plan and revokes the cut armed with it', async () => {
	const { runner, state, dispatched } = await armedLaunchWithPendingCut();
	state.decks[2].quantized_launch_armed = null; // cancelled before its downbeat: the deck never starts
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.deepEqual(dispatched.filter((c) => c.type === 'fader' && c.deck === 1).at(-1), { type: 'fader', deck: 1, value: 1 });
});

test('AGENT-30 ordering: a finish_now whose apply of a pending step is refused still revokes that step on failure', async () => {
	// Sol P1 on #7594: the step left the list before its plain set was tried, so the failure could not revoke it.
	let refusals = 1;
	const { runner, dispatched } = await twoPendingSteps({
		refuse: (c) => c.type === 'eq' && c.band === 'low' && c.value === 0.2 && c.at_context_sec === undefined && refusals-- > 0
	});
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'finish_now' }));
	assert.equal(runner.mirror().status, 'failed');
	const lows = dispatched.filter((c) => c.type === 'eq' && c.band === 'low' && c.at_context_sec === undefined);
	assert.deepEqual(lows.at(-1)?.value, 0.5, 'the refused step is revoked back to its prior value');
});

test('parsePlanOrder carries the compiler notes and refuses malformed ones', () => {
	// Sol P1 #7595: notes are part of the order contract.
	assert.deepEqual(plan.parsePlanOrder(order({ notes: ['outgoing fader uncapped'] })).notes, ['outgoing fader uncapped']);
	assert.deepEqual(plan.parsePlanOrder(order()).notes, [], 'control: an order with no notes to give still carries []');
	// Sol P1 #7595: compile always emits notes, so an order without them is not a compiled order and is refused.
	const { notes: _omitted, ...withoutNotes } = order();
	assert.throws(() => plan.parsePlanOrder(withoutNotes), /plan\.notes must be a list of strings/);
	assert.throws(() => plan.parsePlanOrder(order({ notes: 'x' })), /plan\.notes must be a list of strings/);
	assert.throws(() => plan.parsePlanOrder(order({ notes: [1] })), /plan\.notes must be a list of strings/);
});

test('AGENT-30: an extend whose revoke of one pending step is refused still re-arms the step it did revoke', async () => {
	// Codex #7594: the refusal threw before the unfire loop, so the revoked low stayed marked fired and never ran again.
	let refusals = 1;
	const { runner, dispatched } = await twoPendingSteps({
		refuse: (c) => c.type === 'eq' && c.band === 'high' && c.at_context_sec === undefined && refusals-- > 0
	});
	await assert.rejects(runner.amend({ id: 'plan-test', action: 'extend', bars: 4 }));
	const lowsAhead = () => dispatched.filter((c) => c.type === 'eq' && c.band === 'low' && typeof c.at_context_sec === 'number').length;
	assert.equal(lowsAhead(), 1);
	await runner.tick();
	assert.equal(lowsAhead(), 2, 'the revoked low is unfired and sent ahead again at its bar');
	const highsAhead = dispatched.filter((c) => c.type === 'eq' && c.band === 'high' && typeof c.at_context_sec === 'number').length;
	assert.equal(highsAhead, 1, 'control: the refused high is still waiting in the engine, so it is not sent twice');
});

test('AGENT-30: an off-timeline step whose revoke is refused is still revoked by the failure that follows', async () => {
	// Codex #7594 (the class): it left the list before its revoke was tried, so the failure could not cancel it.
	let refusals = 1;
	const { runner, dispatched, pinAnchor } = await twoPendingSteps({
		refuse: (c) => c.type === 'eq' && c.band === 'high' && c.at_context_sec === undefined && refusals-- > 0
	});
	pinAnchor({ position_ms: 0, context_sec: 5000, pitch: 1 }); // the timeline moved: both steps are now off it
	await runner.tick();
	assert.equal(runner.mirror().status, 'failed');
	assert.match(runner.mirror().reason, /could not be revoked after the clock timeline moved/);
	const highs = dispatched.filter((c) => c.type === 'eq' && c.band === 'high' && c.at_context_sec === undefined);
	assert.deepEqual(highs.map((c) => c.value), [0.5, 0.5], 'the refused revoke, then the failure revoking it again');
});

/** The next plan, two bars on from where twoPendingSteps leaves the clock. */
const nextPlan = () =>
	order({
		id: 'plan-next',
		start_bar: 26,
		events: [
			{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
			{ at_bar: 1, command: { type: 'eq', deck: 1, band: 'high', value: 0.4 } },
			{ at_bar: 4, command: { type: 'master', deck: 2 } },
			{ at_bar: 4, command: { type: 'play', deck: 1, playing: false } }
		]
	});

for (const [path, end] of [
	['abort', (r) => r.runner.amend({ id: 'plan-test', action: 'abort' })],
	['finish_now', (r) => r.runner.amend({ id: 'plan-test', action: 'finish_now' }).catch(() => undefined)],
	['extend then abort', async (r) => {
		await r.runner.amend({ id: 'plan-test', action: 'extend', bars: 4 }).catch(() => undefined);
		await r.runner.amend({ id: 'plan-test', action: 'abort' }).catch(() => undefined);
	}],
	['dispose', (r) => r.runner.dispose()]
]) {
	test(`AGENT-30 (Sol P1 #7594): a step left waiting by a refused revoke at ${path} is revoked before anything replaces the plan`, async () => {
		let refusals = path === 'extend then abort' || path === 'finish_now' ? 2 : 1; // both of that path's tries are refused
		const r = await twoPendingSteps({
			refuse: (c) => c.type === 'eq' && c.band === 'high' && c.at_context_sec === undefined && refusals-- > 0
		});
		await end(r);
		const highId = r.dispatched.find((c) => c.band === 'high' && c.step_id).step_id;
		if (path === 'dispose') {
			assert.ok(r.forgotten.includes(highId) && !r.stepStates.has(highId), 'dispose revokes it, then forgets it');
			return;
		}
		assert.equal(r.stepStates.get(highId), 'waiting', 'the refused revoke left it armed');
		const plainHighs = () => r.dispatched.filter((c) => c.band === 'high' && c.at_context_sec === undefined).length;
		const before = plainHighs();
		r.state.decks[2].playing = false; // the operator stops the old incoming deck before the next plan
		await r.runner.schedule(nextPlan());
		assert.equal(plainHighs(), before + 1, 'revoked before the new plan was accepted');
		assert.ok(r.forgotten.includes(highId) && !r.stepStates.has(highId), 'cancelled, then forgotten');
		assert.equal(r.runner.mirror().plan_id ?? r.runner.mirror().id, 'plan-next');
	});
}

test('AGENT-30 (Sol P1 #7594): an engine reset leaves nothing to revoke, and the next plan is accepted without a revoke', async () => {
	let refusals = 1;
	const r = await twoPendingSteps({ refuse: (c) => c.type === 'eq' && c.band === 'high' && c.at_context_sec === undefined && refusals-- > 0 });
	await r.runner.amend({ id: 'plan-test', action: 'abort' });
	r.state.decks[2].playing = false;
	r.resetEngine();
	const before = r.dispatched.length;
	await r.runner.schedule(nextPlan());
	assert.equal(r.dispatched.filter((c, i) => i >= before && c.band === 'high').length, 0, 'a lost step is not revoked again');
});

test('AGENT-30 (Sol P1 #7594): a refused revoke of the leftover refuses the new plan; control: the new plan steps land once it is revoked', async () => {
	let refusals = 2;
	const r = await twoPendingSteps({ refuse: (c) => c.type === 'eq' && c.band === 'high' && c.at_context_sec === undefined && refusals-- > 0 });
	await r.runner.amend({ id: 'plan-test', action: 'abort' });
	r.state.decks[2].playing = false;
	await assert.rejects(r.runner.schedule(nextPlan()), /plan_unsettled/);
	await r.runner.schedule(nextPlan());
	r.at(25.8);
	await r.runner.tick();
	r.at(26);
	await r.runner.tick();
	r.state.decks[1].position_ms = 26 * BAR_MS - 50;
	await r.runner.tick();
	const mid = r.dispatched.find((c) => c.band === 'high' && String(c.step_id).startsWith('plan-next'));
	assert.ok(mid, 'the new plan sends its step ahead');
	r.advance(200);
	await r.runner.tick();
	assert.equal(r.state.mixer.channels[1].eq_high, 0.4, 'and it lands');
	assert.ok(r.forgotten.includes(mid.step_id), 'then is retired and forgotten');
});

test('AGENT-30 (Sol P1 #7594): the runner forgets a step once it retires it, never while it waits', async () => {
	const r = await twoPendingSteps();
	const ids = r.dispatched.filter((c) => c.step_id).map((c) => c.step_id);
	await r.runner.tick();
	assert.deepEqual(r.forgotten.filter((id) => ids.includes(id)), [], 'both still waiting: nothing forgotten');
	r.advance(200); // both land
	await r.runner.tick();
	assert.deepEqual([...new Set(r.forgotten)].sort(), [...ids].sort(), 'retired, so forgotten');
	// Control: an abort revokes waiting steps, and forgets each once its revoke made it cancelled.
	const a = await twoPendingSteps();
	await a.runner.amend({ id: 'plan-test', action: 'abort' });
	const aIds = a.dispatched.filter((c) => c.step_id).map((c) => c.step_id);
	assert.deepEqual([...new Set(a.forgotten)].sort(), [...aIds].sort());
	for (const id of aIds) assert.equal(a.stepStates.get(id), undefined);
});

for (const [rampBars, sentAhead] of [[3.99, true], [2, true], [4, false], [6, false]]) {
	test(`AGENT-30 (Sol P1 #7594): a fader step at bar 4 ${sentAhead ? 'is sent ahead' : 'waits for the page tick'} when a room ramp ends at bar ${rampBars}`, async () => {
		// One rule: the ramp's end against the step's bar. Ending before it frees the step; ending on it or after holds it.
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(
			order({
				events: [
					{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
					{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: rampBars } },
					{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0.5 } },
					{ at_bar: 8, command: { type: 'master', deck: 2 } },
					{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
				]
			})
		);
		state.mixer.channels[2].fader = 0;
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		state.decks[1].position_ms = 23 * BAR_MS - 50; // bar 3.975: the step is inside the lookahead
		await runner.tick();
		const step = dispatched.find((c) => c.type === 'fader' && c.deck === 1);
		assert.notEqual(runner.mirror().status, 'failed', runner.mirror().reason);
		if (sentAhead) assert.equal(typeof step?.at_context_sec, 'number', 'sent ahead on the audio clock');
		else assert.equal(step, undefined, 'control: held back while the ramp still runs at the bar');
	});
}

test('AGENT-30 (blind review P1 #7594): finish_now applies a step the engine lost since the last tick', async () => {
	const r = await twoPendingSteps();
	r.resetEngine(); // a graph rebuild between ticks: both steps are lost, still marked fired
	await r.runner.amend({ id: 'plan-test', action: 'finish_now' });
	assert.equal(r.state.mixer.channels[1].eq_low, 0.2, 'the lost low step is applied, not skipped');
	assert.equal(r.runner.mirror().status, 'done');
});

test('AGENT-30 (blind review P1 #7594): extend moves a step the engine lost with the rest, not back to its old bar', async () => {
	const r = await twoPendingSteps();
	r.resetEngine();
	await r.runner.amend({ id: 'plan-test', action: 'extend', bars: 4 });
	const lowsSent = () => r.dispatched.filter((c) => c.type === 'eq' && c.band === 'low' && c.value === 0.2).length;
	const before = lowsSent();
	await r.runner.tick(); // still at bar 3.975: its old bar 4 is no longer its bar
	assert.equal(lowsSent(), before, 'not fired at its old bar');
	r.state.decks[1].position_ms = 27 * BAR_MS - 50; // 50 ms before its new bar 8
	await r.runner.tick();
	assert.equal(lowsSent(), before + 1, 'sent at its new bar');
});

test('AGENT-30 (blind review P1 #7594): a step lost during the last tick keeps the plan from completing', async () => {
	let rebuildOnMaster = false;
	const r = rig({ clockBar: 19, onDispatch: (command) => { if (rebuildOnMaster && command.type === 'master') r.resetEngine(); } });
	await r.runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 2 } },
				{ at_bar: 8, command: { type: 'eq', deck: 1, band: 'low', value: 0.2 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	r.at(19.8);
	await r.runner.tick();
	r.at(20);
	await r.runner.tick();
	r.state.decks[1].position_ms = 27 * BAR_MS - 50; // the bar-8 eq step goes ahead
	await r.runner.tick();
	assert.equal(typeof r.dispatched.find((c) => c.type === 'eq')?.at_context_sec, 'number');
	rebuildOnMaster = true; // the graph rebuilds while the bar-8 master dispatch is awaited
	r.lag.holdWrites = true; // its mirror write has not landed yet when the rebuild drops it
	r.advance(60);
	r.state.decks[1].position_ms = 27 * BAR_MS + 10;
	await r.runner.tick();
	assert.notEqual(r.runner.mirror().status, 'done', 'not done: its eq step was lost, not landed');
	rebuildOnMaster = false;
	r.lag.holdWrites = false;
	await r.runner.tick();
	assert.equal(r.state.mixer.channels[1].eq_low, 0.2, 'the next tick applies it');
});

test('AGENT-30 (blind review P1 #7594): a revoke dropped while dispose holds the bus is revoked by dispose itself', async () => {
	const held = { gate: null };
	const r = rig({ clockBar: 19, hold: (command) => (command.type === 'fader' && command.deck === 2 ? held.gate : null) });
	await r.runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 6 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0.2 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	r.state.mixer.channels[2].fader = 0;
	r.at(19.8);
	await r.runner.tick();
	r.at(20);
	await r.runner.tick();
	r.state.decks[1].position_ms = 23 * BAR_MS - 50;
	await r.runner.tick();
	const step = r.dispatched.find((c) => c.type === 'eq' && c.step_id);
	assert.ok(step, 'the eq step went ahead');
	let release;
	held.gate = new Promise((resolve) => (release = resolve));
	r.lag.holdWrites = true; // the step is still waiting when the page closes
	r.advance(250);
	r.state.decks[1].position_ms = 23 * BAR_MS + 200;
	const ticking = r.runner.tick(); // parks on its held ramp dispatch
	await new Promise((resolve) => setImmediate(resolve));
	const disposing = r.runner.dispose(); // closing: the tick's later dispatches are not sent
	r.state.decks[1].stable_id = 'someone-else'; // the tick then fails the plan and tries to revoke the step
	held.gate = null;
	release();
	await ticking;
	await disposing;
	assert.equal(r.runner.mirror().status, 'failed', 'the tick failed the plan while dispose held the bus');
	assert.notEqual(r.stepStates.get(step.step_id), 'waiting', 'dispose revoked it: nothing stays armed after the page closed');
});

/** A plan with two eq steps sent ahead; `held.gate` set to a promise holds every plain eq dispatch on it. */
async function pendingStepsWithHold() {
	const held = { gate: null };
	const r = await twoPendingSteps({ hold: (command) => (command.type === 'eq' && command.at_context_sec === undefined ? held.gate : null) });
	return { ...r, held };
}

for (const [action, extra] of [['extend', { bars: 4 }], ['finish_now', {}], ['abort', {}]]) {
	test(`AGENT-30 ordering: a tick fired mid-${action} waits for the whole amendment`, async () => {
		// Codex BLOCKING #7594 (the class): every amendment is one transaction, so no tick interleaves with its awaits.
		const { runner, dispatched, held, state } = await pendingStepsWithHold();
		let release;
		held.gate = new Promise((resolve) => (release = resolve));
		const amending = runner.amend({ id: 'plan-test', action, ...extra });
		await new Promise((resolve) => setImmediate(resolve)); // the amendment is parked on its first held dispatch
		const before = dispatched.length;
		const statusBefore = runner.mirror().status;
		const ticked = runner.tick();
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(dispatched.length, before, 'the tick dispatched nothing mid-amendment');
		assert.equal(runner.mirror().status, statusBefore, 'and moved no status');
		const barBefore = runner.mirror().bar;
		state.decks[1].position_ms += BAR_MS / 2; // the clock moves on while the amendment holds the tick
		held.gate = null;
		release();
		await amending.catch(() => undefined);
		await ticked;
		for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
		// Sol P1 #7594: the held tick is queued, not dropped: it runs right after the amendment and sees the new bar.
		if (action === 'extend') assert.ok(runner.mirror().bar > barBefore + 0.4, `the queued tick ran (bar ${runner.mirror().bar} vs ${barBefore})`);
		if (action === 'extend') assert.equal(runner.mirror().status, 'running', 'an extended plan is still to run');
		if (action === 'abort') assert.equal(runner.mirror().status, 'aborted');
	});
}

test('AGENT-30 ordering: extending a plan whose last step is pending never lets a tick finish it', async () => {
	// Codex BLOCKING #7594: the tick ran during the revoke, saw nothing pending and every event fired, and marked it done.
	const held = { gate: null };
	const { runner, state, dispatched, at, advance } = rig({
		clockBar: 19,
		hold: (command) => (command.type === 'eq' && command.at_context_sec === undefined ? held.gate : null)
	});
	await runner.schedule(
		order({
			bars: 6,
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 1 } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(21);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	assert.equal(typeof dispatched.find((c) => c.type === 'eq')?.at_context_sec, 'number', 'the eq is pending');
	let release;
	held.gate = new Promise((resolve) => (release = resolve));
	const extending = runner.amend({ id: 'plan-test', action: 'extend', bars: 2 });
	await new Promise((resolve) => setImmediate(resolve));
	advance(200); // the old step time passes while the revoke is held
	const ticked = runner.tick();
	await new Promise((resolve) => setImmediate(resolve));
	held.gate = null;
	release();
	const result = await extending;
	await ticked;
	assert.equal(result.bars, 8);
	assert.equal(runner.mirror().status, 'running', 'still to run its moved step');
});

async function lastStepPending() {
	const r = rig({ clockBar: 19 });
	await r.runner.schedule(
		order({
			bars: 4,
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 0, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 1 } },
				{ at_bar: 3, command: { type: 'master', deck: 2 } },
				{ at_bar: 3, command: { type: 'play', deck: 1, playing: false } },
				{ at_bar: 4, command: { type: 'eq', deck: 1, band: 'low', value: 0 } }
			]
		})
	);
	r.at(19.8);
	await r.runner.tick();
	r.at(21);
	await r.runner.tick();
	r.at(23);
	await r.runner.tick();
	r.state.decks[1].playing = true;
	r.state.decks[1].position_ms = 23 * BAR_MS - 50;
	await r.runner.tick();
	assert.equal(typeof r.dispatched.find((c) => c.type === 'eq')?.at_context_sec, 'number', 'sent ahead');
	return r;
}

test('AGENT-30 ordering: a step whose audio time passed but whose mirror write is still deferred keeps the plan running', async () => {
	// Sol P1 #7594: completion waited on the audio clock only, so a late mirror write let the plan end on the old value.
	const { runner, state, advance, lag } = await lastStepPending();
	lag.holdWrites = true;
	advance(100);
	state.decks[1].position_ms = 23 * BAR_MS + 60;
	await runner.tick();
	assert.equal(runner.mirror().status, 'running', 'not done until the mirror shows the step');
	lag.holdWrites = false;
	await runner.tick();
	assert.equal(runner.mirror().status, 'done', 'done once it lands');
});

test('AGENT-30 ordering: a mirror write delayed beyond any grace still reads waiting; a plain set reads cancelled at once', async () => {
	// Sol P1 #7594: cancellation was inferred from a late mirror write after 0.25 s. The engine owns the state now.
	const { runner, state, advance, lag, plainSet, stepStates, dispatched } = await lastStepPending();
	const id = dispatched.find((c) => c.type === 'eq' && c.at_context_sec !== undefined).step_id;
	lag.holdWrites = true; // a stalled main thread: the write's timer has not run
	advance(10_000);
	state.decks[1].position_ms = 23 * BAR_MS + 300;
	await runner.tick();
	assert.equal(stepStates.get(id), 'waiting');
	assert.equal(runner.mirror().status, 'running', 'still waiting ten seconds late: no grace decides it');
	plainSet({ type: 'eq', deck: 1, band: 'low', value: 0.7 }); // the operator moves that knob
	assert.equal(stepStates.get(id), 'cancelled', 'cancelled the moment the plain set lands');
	await runner.tick();
	assert.equal(runner.mirror().status, 'done', 'a cancelled step holds nothing open');
});

test('AGENT-30 ordering: an open the operator cancelled is not projected, so a cut at its bar is refused as silent', async () => {
	// Sol P1 + Codex BLOCKING #7594: _projected kept the cancelled open, so the outgoing cut passed the silence guard.
	for (const operatorCloses of [true, false]) {
		const { runner, state, dispatched, at } = rig({
			clockBar: 19,
			onDispatch: (command, s, operator) => {
				if (operatorCloses && command.type === 'fader' && command.deck === 2 && command.at_context_sec !== undefined) operator({ type: 'fader', deck: 2, value: 0 });
			}
		});
		await runner.schedule(
			order({
				events: [
					{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
					{ at_bar: 4, command: { type: 'fader', deck: 2, value: 1 } },
					{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
					{ at_bar: 8, command: { type: 'master', deck: 2 } },
					{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
				]
			})
		);
		state.mixer.channels[2].fader = 0.5;
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		state.decks[1].position_ms = 23 * BAR_MS - 50;
		await runner.tick();
		const cut = dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0);
		if (operatorCloses) {
			assert.equal(cut, false, 'not sent ahead against the closed incoming fader');
			state.decks[1].position_ms = 23 * BAR_MS; // its bar: the cut is judged there and refused
			await runner.tick();
			assert.equal(runner.mirror().status, 'failed', 'the cut is judged against the closed incoming fader');
			assert.equal(dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), false);
		} else {
			assert.equal(cut, true, 'control: with the open still pending, the cut goes ahead');
		}
	}
});

test('AGENT-30: a step whose prior value the mirror lacks fails the plan before it is marked fired or sent', async () => {
	// Sol P1 #7594 (the class: read before mutating): the step was marked fired, then the read threw, and a later tick
	// could complete the plan without it.
	for (const valueKnown of [false, true]) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		if (!valueKnown) delete state.mixer.channels[1].eq_low;
		const firedBefore = runner.mirror().fired;
		state.decks[1].position_ms = 23 * BAR_MS - 50;
		await runner.tick();
		const sent = dispatched.some((c) => c.type === 'eq' && c.at_context_sec !== undefined);
		if (valueKnown) {
			assert.equal(sent, true, 'control: with the value known the step is sent ahead');
		} else {
			assert.equal(sent, false);
			assert.equal(runner.mirror().status, 'failed');
			assert.equal(runner.mirror().fired, firedBefore, 'the unsent step is never counted as fired');
			assert.match(runner.mirror().reason, /no current value for eq_low/);
		}
	}
});

test('AGENT-30: two runners never generate the same step id for the same plan', async () => {
	// Codex + Sol #7594: a runner recreated after a remount restarted its sequence and reused the old ids.
	const ids = [];
	for (let i = 0; i < 2; i += 1) {
		const { runner, state, dispatched, at } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		state.decks[1].position_ms = 23 * BAR_MS - 50;
		await runner.tick();
		ids.push(dispatched.find((c) => c.step_id !== undefined).step_id);
	}
	assert.notEqual(ids[0], ids[1]);
});

test('AGENT-30: a step sent ahead is revoked and re-fired when the clock timeline moves before it lands', async () => {
	// Sol P1 #7594: a seek, tempo change, pause or loop during the lookahead left the automation armed at the old time.
	for (const change of ['none', 'seek', 'anchor-lost']) {
		const { runner, state, dispatched, at, advance, pinAnchor } = rig({ clockBar: 19 });
		await runner.schedule(order());
		at(19.8);
		await runner.tick();
		at(20);
		await runner.tick();
		state.mixer.channels[1].eq_low = 0.5;
		state.decks[1].position_ms = 23 * BAR_MS - 80;
		await runner.tick();
		const sent = dispatched.filter((c) => c.type === 'eq' && c.at_context_sec !== undefined);
		assert.equal(sent.length, 1, `${change}: the kill is sent ahead`);
		advance(10);
		if (change === 'none') state.decks[1].position_ms += 10; // the playhead moves with the audio clock: same timeline
		if (change === 'seek') state.decks[1].position_ms -= 2000; // the DJ seeks back two seconds
		if (change === 'anchor-lost') pinAnchor(null); // a loop, pause or tempo change: the anchor reads null
		await runner.tick();
		const revokes = dispatched.filter((c) => c.type === 'eq' && c.at_context_sec === undefined && c.value === 0.5);
		if (change === 'none') {
			assert.equal(revokes.length, 0, 'control: an unchanged timeline keeps the step');
		} else {
			assert.equal(revokes.length, 1, `${change}: the step is revoked to the value it replaced`);
			assert.notEqual(runner.mirror().status, 'failed');
			assert.ok(runner.mirror().fired < runner.mirror().total, `${change}: the step is unfired, to be sent again or fired on its bar`);
		}
	}
});

test('AGENT-30: an outgoing cut is not sent ahead while an opening ramp on the incoming fader still runs into its bar', async () => {
	// Codex BLOCKING #7594: the cut was judged at send time against the closed incoming fader and failed the plan.
	const { runner, state, dispatched, at, advance } = rig({ clockBar: 19 });
	await runner.schedule(
		order({
			events: [
				{ at_bar: -0.25, command: { type: 'play', deck: 2, playing: true, quantize: true } },
				{ at_bar: 3.98, ramp: { command: { type: 'fader', deck: 2, value: 1 }, to: 1, over_bars: 0.02 } },
				{ at_bar: 4, command: { type: 'fader', deck: 1, value: 0 } },
				{ at_bar: 8, command: { type: 'master', deck: 2 } },
				{ at_bar: 8, command: { type: 'play', deck: 1, playing: false } }
			]
		})
	);
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 35; // bar 3.982: the ramp has started, the cut is inside the lookahead
	await runner.tick();
	assert.notEqual(runner.mirror().status, 'failed', runner.mirror().reason);
	assert.equal(dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.at_context_sec !== undefined), false, 'not sent ahead');
	advance(40);
	state.decks[1].position_ms = 23 * BAR_MS + 5; // on its bar, after the ramp opened the incoming fader
	await runner.tick();
	assert.ok(dispatched.some((c) => c.type === 'fader' && c.deck === 1 && c.value === 0), 'the cut fires on its bar');
	assert.notEqual(runner.mirror().status, 'failed', runner.mirror().reason);
});

test('AGENT-30: a step lost with the engine session (graph rebuild) is fired again, not skipped', async () => {
	// Codex BLOCKING #7594: the rebuild cleared the step's state, it dropped out of the pending list still marked
	// fired, and the plan completed without the kill. Control: a step an operator cancelled stays the operator's.
	for (const how of ['rebuild', 'operator']) {
		const { runner, state, dispatched, advance, resetEngine, plainSet } = await lastStepPending();
		const sent = dispatched.find((c) => c.type === 'eq' && c.at_context_sec !== undefined);
		if (how === 'rebuild') resetEngine(); // the new engine session has never seen it
		if (how === 'operator') plainSet({ type: 'eq', deck: 1, band: 'low', value: 0.7 });
		const before = dispatched.length;
		advance(100);
		state.decks[1].position_ms = 23 * BAR_MS + 60;
		await runner.tick();
		const again = dispatched.slice(before).filter((c) => c.type === 'eq' && c.band === 'low' && c.value === 0);
		if (how === 'rebuild') {
			assert.equal(again.length, 1, 'the lost kill is fired again');
			assert.notEqual(again[0].step_id, sent.step_id);
		} else {
			assert.equal(again.length, 0, 'control: the operator owns that control now');
		}
		assert.equal(runner.mirror().status, 'done');
	}
});

test('AGENT-30: a step that already landed is never re-fired after a later engine reset', async () => {
	// Codex BLOCKING #7594: the reset cleared every state, so a landed kill read as lost and overwrote the operator.
	const { runner, state, dispatched, at, advance, stepStates, plainSet } = rig({ clockBar: 19 });
	await runner.schedule(order()); // the eq kill at bar 4, the handover at bar 8: the plan runs on after it
	at(19.8);
	await runner.tick();
	at(20);
	await runner.tick();
	state.decks[1].position_ms = 23 * BAR_MS - 50;
	await runner.tick();
	advance(100);
	state.decks[1].position_ms = 23 * BAR_MS + 60;
	await runner.tick(); // the kill lands and the plan sees it landed
	plainSet({ type: 'eq', deck: 1, band: 'low', value: 0.7 }); // the operator reopens the low EQ
	stepStates.clear(); // then the graph rebuilds, here as an engine that keeps no state at all (the worst case)
	const before = dispatched.length;
	advance(100);
	state.decks[1].position_ms = 23 * BAR_MS + 300;
	await runner.tick();
	assert.equal(runner.mirror().status, 'running');
	assert.equal(dispatched.slice(before).filter((c) => c.type === 'eq' && c.band === 'low').length, 0, 'the landed kill is not sent again');
	assert.equal(state.mixer.channels[1].eq_low, 0.7);
});
