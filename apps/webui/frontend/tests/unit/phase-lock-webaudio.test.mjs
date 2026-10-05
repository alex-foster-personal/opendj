/**
 * Continuous Beat Sync phase lock in the Web Audio engine (NAE-19): the
 * bookkeeping module `rb/phase-lock-webaudio.ts` driven through a fake engine
 * whose positions integrate the scheduled tempo on a fake audio clock, plus
 * source-text wiring guards on `audio-engine.svelte.ts` (the engine's tick and
 * sync are module-private and not drivable from node).
 *
 * Regression lines:
 * - if a trim is not relative to the recorded base (accumulates) then broken
 * - if a lock trims while either deck's schedule is unsettled then broken: it
 *   would race `_synchronizeFollowers` and the re-anchor ramps
 * - if a lock survives a master change, Beat Sync off, a stop, a reload, a
 *   master tempo move or a tempo written by anything else then broken
 * - if a lost lock is trimmed instead of re-joined then broken
 * - if a follower whose grid runs 0.05% off its base is not held within 5 ms
 *   for five minutes then broken; the control without ticks must drift past
 *   100 ms, or the simulation is not testing anything
 * - if the engine's presentation tick stops calling the lock, or sync stops
 *   recording/clearing it, then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let pl;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/phase-lock-webaudio.ts');
	pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
});

function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

const MASTER_GRID = grid(128, 0.1, 2000);
const FOLLOWER_GRID = grid(126, 0.05, 2000);
const BASE = 128 / 126;

/** Follower position in phase with a master position (normalization 1). */
function inPhase(mSec) {
	const m = pl.gridBeatPosition(MASTER_GRID, mSec);
	const i = Math.floor(m);
	return FOLLOWER_GRID[i].t + (m - i) * (FOLLOWER_GRID[i + 1].t - FOLLOWER_GRID[i].t);
}

/**
 * A fake engine: deck 1 masters deck 2. Each deck's position integrates its
 * scheduled tempo times `rateError` (the audio running off what the grid says)
 * on the fake clock; a tempo schedule lands at once.
 */
function fakeEngine({ followerRateError = 0, scheduleDelaySync = true } = {}) {
	const decks = {
		1: { pos: 60, tempo: 1, rate: 1, playing: true, owns: false, loadToken: 1, id: 'm', settled: true, loop: false, beats: MASTER_GRID },
		2: { pos: inPhase(60), tempo: BASE, rate: 1 + followerRateError, playing: true, owns: true, loadToken: 1, id: 'f', settled: true, loop: false, beats: FOLLOWER_GRID },
		3: { pos: 0, tempo: 1, rate: 1, playing: false, owns: false, loadToken: 0, id: null, settled: true, loop: false, beats: [] },
		4: { pos: 0, tempo: 1, rate: 1, playing: false, owns: false, loadToken: 0, id: null, settled: true, loop: false, beats: [] }
	};
	const log = { schedules: [], resyncs: [], errors: [] };
	let master = 1;
	let now = 0;
	const ports = {
		deckIds: [1, 2, 3, 4],
		syncMaster: () => master,
		masterDeck: () => master,
		ownsTempo: (d) => decks[d].owns,
		playing: (d) => decks[d].playing,
		loadToken: (d) => decks[d].loadToken,
		stableId: (d) => decks[d].id,
		settled: (d) => decks[d].settled,
		desiredTempo: (d) => decks[d].tempo,
		beats: (d) => decks[d].beats,
		positionSec: (d, t) => {
			assert.equal(t, now, 'positions are read at the tick context time');
			return decks[d].pos;
		},
		pitchRangePct: () => 8,
		loopEngaged: (d) => decks[d].loop,
		scheduleTempo: (d, ratio) => {
			log.schedules.push({ deck: d, ratio });
			if (scheduleDelaySync) decks[d].tempo = ratio;
			return Promise.resolve(0);
		},
		resync: (m, d) => {
			log.resyncs.push({ master: m, deck: d });
			return Promise.resolve();
		},
		reportError: (d, message) => log.errors.push({ deck: d, message })
	};
	return {
		decks,
		log,
		ports,
		setMaster: (m) => (master = m),
		get now() {
			return now;
		},
		clockOnly(dt) {
			now += dt;
		},
		advance(dt) {
			now += dt;
			for (const d of Object.values(decks)) if (d.playing) d.pos += dt * d.tempo * d.rate;
		}
	};
}

const JOIN = { master: 1, masterTempo: 1, base: BASE, normalization: 1 };

/** Let `.then` / `.finally` of an in-flight trim run. */
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('in phase: the base, nothing scheduled', () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	const steps = lock.tick(e.now);
	assert.equal(steps.length, 1);
	assert.equal(steps[0].kind, 'decided');
	assert.equal(steps[0].decision.action, 'base');
	assert.equal(steps[0].sent, false);
	assert.deepEqual(e.log.schedules, []);
});

test('ahead: a trim below the RECORDED base goes through scheduleTempo, then returns to exactly base', async () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	e.decks[2].pos += 0.01 * BASE; // 10 ms ahead
	const [step] = lock.tick(e.now);
	assert.equal(step.decision.action, 'trim');
	assert.equal(e.log.schedules.length, 1);
	const trimmed = e.log.schedules[0].ratio;
	assert.ok(trimmed < BASE && trimmed >= BASE * (1 - pl.PHASE_LOCK_MAX_TRIM) - 1e-12, `trim ${trimmed}`);
	assert.equal(lock.snapshot().get(2).busy, true);
	// Busy: the next tick waits for the in-flight trim.
	e.advance(0.05);
	assert.deepEqual(lock.tick(e.now).map((s) => s.kind), ['waiting']);
	await flush();
	assert.equal(lock.snapshot().get(2).sent, trimmed);
	assert.equal(lock.snapshot().get(2).base, BASE, 'the base never moves with a trim');
	// Back in phase: exactly the base again, never base * (1 + trim).
	e.decks[2].pos = inPhase(e.decks[1].pos);
	e.advance(0.05);
	const [back] = lock.tick(e.now);
	assert.equal(back.decision.action, 'base');
	assert.equal(e.log.schedules.at(-1).ratio, BASE);
});

test('a second trim is relative to the base, not to the first trim', async () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	const mSec = e.decks[1].pos;
	e.decks[2].pos = inPhase(mSec) + 0.004 * BASE; // 4 ms ahead
	lock.tick(e.now);
	await flush();
	const first = e.log.schedules[0].ratio;
	assert.ok(first < BASE);
	// Same instant on the grid, twice the error; only the clock moves.
	e.decks[2].pos = inPhase(mSec) + 0.008 * BASE;
	e.clockOnly(0.05);
	lock.tick(e.now);
	const expected = pl.phaseLockDecision({
		masterBeats: MASTER_GRID,
		masterPositionSec: mSec,
		masterTempo: 1,
		followerBeats: FOLLOWER_GRID,
		followerPositionSec: e.decks[2].pos,
		followerBaseTempo: BASE,
		normalization: 1,
		pitchRangePct: 8,
		trimming: true,
		sinceJoinSec: 0.05,
		overLineTicks: 0
	});
	assert.equal(expected.action, 'trim');
	assert.equal(e.log.schedules.length, 2);
	assert.equal(e.log.schedules[1].ratio, expected.tempo, 'base * (1 + trim), from the recorded base');
	assert.notEqual(e.log.schedules[1].ratio, (first / BASE) * expected.tempo, 'not compounded on the first trim');
});

test('throttled to 30 Hz of AudioContext time, however fast the frames come', () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	assert.equal(lock.tick(e.now).length, 1);
	e.advance(1 / 120);
	assert.equal(lock.tick(e.now).length, 0, 'a 120 Hz frame inside the interval is skipped');
	e.advance(mod.PHASE_LOCK_WEBAUDIO_INTERVAL_SEC);
	assert.equal(lock.tick(e.now).length, 1);
});

test('unsettled schedule on either deck: the lock waits, keeps its base, sends nothing', () => {
	for (const unsettled of [1, 2]) {
		const e = fakeEngine();
		const lock = mod.createWebAudioPhaseLock(e.ports);
		lock.record(2, JOIN);
		e.decks[2].pos += 0.02 * BASE;
		e.decks[unsettled].settled = false;
		const [step] = lock.tick(e.now);
		assert.deepEqual([step.kind, step.reason], ['waiting', 'unsettled'], `deck ${unsettled}`);
		assert.deepEqual(e.log.schedules, []);
		assert.ok(lock.snapshot().has(2), 'kept, not dropped');
	}
});

test('supersede: every broken assumption drops the lock and sends nothing', () => {
	const cases = {
		'master change': (e) => e.setMaster(3),
		'no master': (e) => e.setMaster(null),
		'Beat Sync off': (e) => (e.decks[2].owns = false),
		stopped: (e) => (e.decks[2].playing = false),
		reload: (e) => (e.decks[2].loadToken += 1),
		'another track': (e) => (e.decks[2].id = 'other'),
		'master tempo moved': (e) => (e.decks[1].tempo = 1.01),
		're-sync or ramp wrote the follower tempo': (e) => (e.decks[2].tempo = BASE * 1.001)
	};
	for (const [name, mutate] of Object.entries(cases)) {
		const e = fakeEngine();
		const lock = mod.createWebAudioPhaseLock(e.ports);
		lock.record(2, JOIN);
		e.decks[2].pos += 0.02 * BASE;
		mutate(e);
		const [step] = lock.tick(e.now);
		assert.equal(step.kind, 'dropped', name);
		assert.equal(lock.snapshot().has(2), false, name);
		assert.deepEqual(e.log.schedules, [], name);
		assert.deepEqual(e.log.resyncs, [], name);
	}
});

test('control: an unbroken lock is NOT dropped (the supersede checks can say no)', () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	for (let i = 0; i < 30; i++) {
		e.advance(0.05);
		lock.tick(e.now);
	}
	assert.ok(lock.snapshot().has(2));
});

/** A lock with a trim in force (10 ms ahead, settled). */
async function trimmingLock() {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	e.decks[2].pos += 0.01 * BASE;
	lock.tick(e.now);
	await flush();
	const trimmed = lock.snapshot().get(2).sent;
	assert.ok(trimmed < BASE, 'precondition: a slow-down trim is in force');
	assert.equal(e.decks[2].tempo, trimmed, 'precondition: the deck plays the trim');
	e.log.schedules.length = 0;
	e.advance(0.05);
	return { e, lock, trimmed };
}

test('release: a lock dropped mid-trim schedules its base back once (no lasting 0.3% offset)', async () => {
	for (const [name, mutate] of Object.entries({
		'Beat Sync off': (e) => (e.decks[2].owns = false),
		'no playing master': (e) => e.setMaster(null)
	})) {
		const { e, lock } = await trimmingLock();
		mutate(e);
		const [step] = lock.tick(e.now);
		assert.deepEqual([step.kind, step.released], ['dropped', true], name);
		assert.deepEqual(e.log.schedules, [{ deck: 2, ratio: BASE }], name);
		e.advance(0.05);
		lock.tick(e.now);
		assert.equal(e.log.schedules.length, 1, `${name}: released once, the lock is gone`);
	}
});

test('control: a dropped trim is NOT released when the tempo is no longer the lock\'s', async () => {
	const cases = {
		// #1134: a promoted deck's tempo is the master's; its followers lock to it.
		'promoted to master': (e) => {
			e.setMaster(2);
			e.decks[2].owns = false;
		},
		// The schedule path starts transport: a stopped deck is never scheduled.
		stopped: (e) => (e.decks[2].playing = false),
		reload: (e) => (e.decks[2].loadToken += 1),
		'another track': (e) => (e.decks[2].id = 'other'),
		'tempo written elsewhere': (e) => {
			e.decks[2].owns = false;
			e.decks[2].tempo = 1.02;
		}
	};
	for (const [name, mutate] of Object.entries(cases)) {
		const { e, lock } = await trimmingLock();
		mutate(e);
		const [step] = lock.tick(e.now);
		assert.equal(step.kind, 'dropped', name);
		assert.equal(step.released, undefined, name);
		assert.deepEqual(e.log.schedules, [], name);
	}
});

test('a failed release is reported, not thrown into the frame', async () => {
	const { e, lock } = await trimmingLock();
	e.ports.scheduleTempo = () => Promise.reject(new Error('processor gone'));
	e.decks[2].owns = false;
	lock.tick(e.now);
	await flush();
	assert.deepEqual(e.log.errors, [{ deck: 2, message: 'phase lock release failed: processor gone' }]);
});

test('feed-forward: a tempo change inside the follower grid moves the base, and the trim rides on it', async () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	// The follower is now in a 130 BPM region of its grid, still in phase.
	const region = grid(130, 0.05, 2000);
	const m = pl.gridBeatPosition(MASTER_GRID, e.decks[1].pos);
	const i = Math.floor(m);
	e.decks[2].beats = region;
	e.decks[2].pos = region[i].t + (m - i) * (region[i + 1].t - region[i].t);
	const [step] = lock.tick(e.now);
	assert.equal(step.decision.action, 'base');
	assert.ok(Math.abs(lock.snapshot().get(2).base - 128 / 130) < 1e-9, `base ${lock.snapshot().get(2).base}`);
	assert.deepEqual(e.log.schedules.map((x) => x.deck), [2]);
	assert.ok(Math.abs(e.log.schedules[0].ratio - 128 / 130) < 1e-9, 'the new base is scheduled');
	await flush();
	// Control: on a constant grid the base never moves (no command at all).
	e.log.schedules.length = 0;
	for (let k = 0; k < 30; k++) {
		e.advance(0.05);
		lock.tick(e.now);
		await flush();
	}
	assert.ok(Math.abs(lock.snapshot().get(2).base - 128 / 130) < 1e-9);
	assert.deepEqual(e.log.schedules, []);
});

test('a trim in flight is superseded by a re-sync: its late completion does not touch the new lock', async () => {
	const e = fakeEngine({ scheduleDelaySync: false });
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	e.decks[2].pos += 0.01 * BASE;
	lock.tick(e.now);
	assert.equal(e.log.schedules.length, 1);
	// A re-sync clears and re-records at a new base before the trim resolves.
	lock.clear(2);
	lock.record(2, { ...JOIN, base: BASE * 1.001 });
	await flush();
	const fresh = lock.snapshot().get(2);
	assert.equal(fresh.sent, BASE * 1.001, 'the stale trim did not overwrite the new lock');
	assert.equal(fresh.busy, false);
});

test('a failed trim drops the lock and reports it', async () => {
	const e = fakeEngine();
	e.ports.scheduleTempo = () => Promise.reject(new Error('processor gone'));
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	e.decks[2].pos += 0.01 * BASE;
	lock.tick(e.now);
	await flush();
	assert.equal(lock.snapshot().has(2), false);
	assert.match(e.log.errors[0].message, /trim failed: processor gone/);
});

/** A lock old enough to re-join: in phase and ticking for the minimum interval. */
async function agedLock(e) {
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	for (let elapsed = 0; elapsed <= pl.PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC; elapsed += 0.05) {
		lock.tick(e.now);
		e.advance(0.05);
	}
	await flush();
	assert.deepEqual(e.log.schedules, [], 'precondition: in phase, nothing scheduled');
	return lock;
}

test('a lost lock re-joins through resync once, after confirming; a looped follower is left alone', async () => {
	const e = fakeEngine();
	const lock = await agedLock(e);
	e.decks[2].pos = inPhase(e.decks[1].pos) + (0.3 * 60) / 128 * BASE; // 0.3 beat ahead
	// The confirming ticks trim at the cap; nothing is seeked on one reading.
	for (let tick = 1; tick < pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS; tick++) {
		const [step] = lock.tick(e.now);
		assert.equal(step.decision.action, 'trim', `tick ${tick}: confirming, trimmed at the cap`);
		assert.deepEqual(e.log.resyncs, [], `tick ${tick}: not re-joined yet`);
		await flush();
		e.clockOnly(0.05);
	}
	let step;
	for (let tick = 0; tick < 4 && e.log.resyncs.length === 0; tick++) {
		[step] = lock.tick(e.now);
		await flush();
		e.clockOnly(0.05);
	}
	assert.equal(step.decision.action, 'reseek');
	assert.deepEqual(e.log.resyncs, [{ master: 1, deck: 2 }]);
	assert.equal(lock.snapshot().has(2), false, 'dropped until the join records a new one');
	e.advance(0.05);
	lock.tick(e.now);
	assert.equal(e.log.resyncs.length, 1, 'no second re-seek while the first is in flight');

	const looped = fakeEngine();
	const lock2 = await agedLock(looped);
	looped.decks[2].pos = inPhase(looped.decks[1].pos) + (0.3 * 60) / 128 * BASE;
	looped.decks[2].loop = true;
	for (let tick = 0; tick < 10; tick++) {
		lock2.tick(looped.now);
		await flush();
		looped.clockOnly(0.05);
	}
	assert.deepEqual(looped.log.resyncs, []);
	assert.ok(lock2.snapshot().has(2));
});

test('no re-join inside the minimum interval after a join: the capped trim works on it instead', async () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	e.decks[2].pos += 0.03 * BASE; // 30 ms ahead: past the re-join line
	let lastTrim = null;
	for (let elapsed = 0; elapsed < pl.PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC - 0.1; elapsed += 0.05) {
		lock.tick(e.now);
		await flush();
		e.advance(0.05);
		lastTrim = e.log.schedules.at(-1)?.ratio ?? lastTrim;
	}
	assert.deepEqual(e.log.resyncs, [], 'never seeked inside the interval');
	assert.ok(Math.abs(e.log.schedules[0].ratio - BASE * (1 - pl.PHASE_LOCK_MAX_TRIM)) < 1e-12, 'trimmed at the cap');
	const left = pl.phaseErrorMs({
		masterBeats: MASTER_GRID,
		masterPositionSec: e.decks[1].pos,
		masterTempo: 1,
		followerBeats: FOLLOWER_GRID,
		followerPositionSec: e.decks[2].pos
	});
	assert.ok(Math.abs(left) < pl.PHASE_LOCK_RESEEK_MS, `walked back to ${left.toFixed(1)} ms without a seek`);
	// Mutation guard for the gate itself: the SAME error on an aged lock does re-join.
	const aged = fakeEngine();
	const lock2 = await agedLock(aged);
	aged.decks[2].pos += 0.03 * BASE;
	for (let tick = 0; tick < 8; tick++) {
		lock2.tick(aged.now);
		await flush();
		aged.clockOnly(0.05);
	}
	assert.equal(aged.log.resyncs.length, 1);
});

test('nudge: an offset the DJ dialed in is held; without the nudge the same offset is trimmed back', async () => {
	const run = async (tell) => {
		const e = fakeEngine();
		const lock = mod.createWebAudioPhaseLock(e.ports);
		lock.record(2, JOIN);
		e.decks[2].pos += 0.008 * BASE; // the jog moved the deck 8 ms ahead
		if (tell) lock.nudge(2, 8);
		for (let i = 0; i < 400; i++) {
			lock.tick(e.now);
			await flush();
			e.advance(0.05);
		}
		const standing = pl.phaseErrorMs({
			masterBeats: MASTER_GRID,
			masterPositionSec: e.decks[1].pos,
			masterTempo: 1,
			followerBeats: FOLLOWER_GRID,
			followerPositionSec: e.decks[2].pos
		});
		return { e, standing };
	};
	const held = await run(true);
	assert.deepEqual(held.e.log.schedules, [], 'no trim fights the nudge');
	assert.deepEqual(held.e.log.resyncs, []);
	assert.ok(Math.abs(held.standing - 8) < 1e-6, `still ${held.standing} ms ahead`);
	const fought = await run(false);
	assert.ok(fought.e.log.schedules.length > 0, 'an offset nobody dialed in is corrected');
	assert.ok(Math.abs(fought.standing) < pl.PHASE_LOCK_DEADBAND_MS, `pulled back to ${fought.standing} ms`);
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	assert.throws(() => lock.nudge(2, 8), /no lock/);
	lock.record(2, JOIN);
	assert.throws(() => lock.nudge(2, Number.NaN), /finite/);
});

test('a malformed input drops that lock and reports it instead of throwing into the frame', () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	e.ports.positionSec = () => {
		throw new Error('audio graph not initialised');
	};
	assert.doesNotThrow(() => lock.tick(e.now));
	assert.equal(lock.snapshot().has(2), false);
	assert.match(e.log.errors[0].message, /audio graph not initialised/);
});

test('record refuses a self-follow and a non-positive base; clearAll empties', () => {
	const e = fakeEngine();
	const lock = mod.createWebAudioPhaseLock(e.ports);
	assert.throws(() => lock.record(1, JOIN), RangeError);
	assert.throws(() => lock.record(2, { ...JOIN, base: 0 }), RangeError);
	lock.record(2, JOIN);
	lock.clearAll();
	assert.equal(lock.snapshot().size, 0);
});

/** 300 s at 60 fps through the module; returns the worst |error| after 10 s. */
async function simulate(rateError, { ticking = true } = {}) {
	const e = fakeEngine({ followerRateError: rateError });
	const lock = mod.createWebAudioPhaseLock(e.ports);
	lock.record(2, JOIN);
	let worst = 0;
	for (let i = 0; i < 300 * 60; i++) {
		e.advance(1 / 60);
		if (ticking) {
			lock.tick(e.now);
			if (i % 6 === 0) await flush();
		}
		if (e.now > 10) {
			const err = pl.phaseErrorMs({
				masterBeats: MASTER_GRID,
				masterPositionSec: e.decks[1].pos,
				masterTempo: 1,
				followerBeats: FOLLOWER_GRID,
				followerPositionSec: e.decks[2].pos,
				followerBaseTempo: BASE,
				pitchRangePct: 8
			});
			if (err !== null) worst = Math.max(worst, Math.abs(err));
		}
	}
	return { worst, lock, e };
}

test('simulation: a follower running 0.05% off its base is held under 5 ms for five minutes', async () => {
	const { worst, lock, e } = await simulate(0.0005);
	console.log(`# phase-lock-webaudio sim 0.05%: worst ${worst.toFixed(2)} ms, ${e.log.schedules.length} trims in 300 s`);
	assert.ok(worst < 5, `worst ${worst.toFixed(2)} ms`);
	assert.ok(lock.snapshot().has(2), 'the lock held throughout');
	assert.ok(e.log.schedules.length > 0, 'it trimmed');
	assert.deepEqual(e.log.resyncs, []);
});

test('control: without ticks the same 0.05% drifts past 100 ms', async () => {
	const { worst } = await simulate(0.0005, { ticking: false });
	assert.ok(worst > 100, `worst ${worst.toFixed(2)} ms`);
});

test('cost: one tick with three locked followers stays well under a frame budget', () => {
	const e = fakeEngine();
	for (const d of [3, 4]) {
		Object.assign(e.decks[d], { playing: true, owns: true, loadToken: 1, id: `f${d}`, beats: FOLLOWER_GRID, tempo: BASE, pos: inPhase(60) + 0.004 });
	}
	const lock = mod.createWebAudioPhaseLock(e.ports);
	for (const d of [2, 3, 4]) lock.record(d, JOIN);
	const N = 2000;
	const t0 = performance.now();
	for (let i = 0; i < N; i++) {
		e.advance(mod.PHASE_LOCK_WEBAUDIO_INTERVAL_SEC);
		lock.tick(e.now);
	}
	const perTickMs = (performance.now() - t0) / N;
	console.log(`# phase-lock-webaudio tick: ${(perTickMs * 1000).toFixed(1)} us per tick, 3 followers`);
	assert.ok(perTickMs < 0.5, `${perTickMs} ms per tick`);
});

//-----------------------------------------------------------------------------
// wiring in audio-engine.svelte.ts (source-level: _tick and
// _synchronizeFollowers are module-private and not drivable under node)
//-----------------------------------------------------------------------------

test('SOURCE: the presentation tick runs the phase lock on the audio clock', () => {
	const tick = engineBlockAfter('function _tick(): void {');
	assert.match(tick, /_phaseLock\.tick\(_ctx\.currentTime\)/);
});

test('SOURCE: sync clears the lock on entry and records the plan base on success', () => {
	const sync = engineBlockAfter('async function _synchronizeFollowers(\n\tmaster: DeckId,\n\tfollowers: readonly DeckId[],\n\toptions: _SyncOptions = {}\n): Promise<void> {');
	const clearAt = sync.indexOf('_phaseLock.clear(deck)');
	const firstAwait = sync.indexOf('await ');
	assert.ok(clearAt >= 0 && clearAt < firstAwait, 'cleared before the first await');
	assert.match(
		sync,
		/_phaseLock\.record\(item\.deck, \{ master, masterTempo: masterTempoRatio, base: item\.plan\.followerTempoRatio, normalization: item\.plan\.tempoNormalization \}\)/
	);
	const dispose = engineBlockAfter('async dispose(): Promise<void> {');
	assert.match(dispose, /_phaseLock\.clearAll\(\)/);
});

test('SOURCE: trims and re-seeks go through the engine schedule and sync paths', () => {
	const ports = engineBlockAfter('const _phaseLock = createWebAudioPhaseLock({');
	assert.match(ports, /ownsTempo: _syncOwnsFollowerTempo/);
	assert.match(ports, /masterDeck: _ownedMaster,/, 'the release reads the master ROLE, not only a playing master');
	assert.match(ports, /positionSec: _projectPositionAt/);
	assert.match(ports, /scheduleTempo: \(deck, ratio\) => _scheduleDeck\(deck, _futureScheduleTime\(deck\), \(when\) => _projectPositionAt\(deck, when\), true, ratio\)/);
	assert.match(ports, /resync: \(master, deck\) => _synchronizeFollowers\(master, \[deck\]\)/);
	for (const settled of ['rt.pending.length === 0', 'rt.scheduleIntentCount === 0', '!_presentationPending(rt)', '!_reanchorRampPending(rt)', '_quantizedLaunchAt[deck] === null']) {
		assert.ok(ports.includes(settled), `settled() checks ${settled}`);
	}
});
