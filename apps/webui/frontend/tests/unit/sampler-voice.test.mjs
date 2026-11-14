/**
 * Issue #10780 (S3): the sampler's four voices and its cue preview (spec 4.2, 4.3, 4.4), driven
 * through fake ports: the launch math, steal, toggle, free run and master follow are the bank's own.
 *
 * Regression lines:
 * - if a hot slot fires with a playing master then it starts on the next downbeat with no await before start
 * - if no master plays then the voice runs at rate 1 and reports free
 * - if four voices sound and a fifth fires then the oldest is stopped and reused
 * - if a sounding slot fires again then it stops
 * - if a slot previews then it plays on the cue bus only, on the cue clock
 * - if the master tempo moves then a matched voice follows on the next tick
 * - if the master seeks while a loop sounds then the voice re-phases at the next beat inside its loop window
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let B;
before(async () => {
	B = await loadTypeScriptModule('src/lib/player/sampler/sampler-voice-bank.ts');
});

/** 128 frames at 48 kHz: one render quantum. */
const QUANTUM_SEC = 128 / 48_000;

const slot = (id, settings = {}) => ({
	slot_id: id,
	native_bpm: 120,
	key: '8A',
	in_ms: 10_000,
	out_ms: 12_000,
	beats: 4,
	vector: null,
	stems_ready: false,
	settings: {
		launch: 'bar',
		key: 'auto',
		tempo: 'match',
		mode: 'loop',
		repeat: 'loop',
		downbeat_offset: 0,
		level: 80,
		...settings
	}
});

/** A 120 BPM master grid (beat every 0.5 s, bar every 2 s) playing at `positionSec` when the clock reads 10. */
function master(bpm = 120, positionSec = 3.3) {
	const beats = Array.from({ length: 64 }, (_, i) => ({ n: (i % 4) + 1, t: i * 0.5 }));
	return {
		deck: 1,
		bpm,
		key: '8A',
		beats,
		at: (now) => ({ positionSec: positionSec + (now - 10), tempoRatio: 1 })
	};
}

function rig(opts = {}) {
	const clock = { now: 10, master: opts.master ?? null, launchFresh: false };
	const made = [];
	const ports = {
		now: () => clock.now,
		master: () => {
			if (clock.master === null) return null;
			const m = clock.master;
			return {
				...m,
				at: (now) => {
					// Flag the launch computation; any await before start() runs this microtask first.
					clock.launchFresh = true;
					queueMicrotask(() => (clock.launchFresh = false));
					return m.at(now);
				}
			};
		},
		buffer: async (s, bus) => ({ slot: s.slot_id, bus }),
		voice: async (id, plan, bus) => {
			const v = {
				id,
				plan,
				bus,
				starts: [],
				stops: 0,
				rates: [],
				rephases: [],
				applyVector() {},
				rephase(at, clipSec) {
					v.rephases.push([at, clipSec]);
				},
				start(at) {
					v.starts.push({ at, fresh: clock.launchFresh });
				},
				setRate(rate, semitones) {
					v.rates.push([rate, semitones]);
				},
				stop() {
					v.stops += 1;
				}
			};
			made.push(v);
			return v;
		},
		holdsClip: () => false,
		cueOffsetSec: () => 0.25,
		report: (what, e) => {
			throw new Error(`${what}: ${e}`);
		}
	};
	const state = { voices: B.VOICE_IDS.map(B.idleVoiceState), preview: null };
	return { bank: new B.SamplerVoiceBank(ports, state), state, clock, made };
}

const timers = (t) => t.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });

test('downbeat launch, no await', async (t) => {
	/** [if] a hot slot fires against a playing master mid-bar [then] start is the next downbeat, synchronously, [else stop]. */
	timers(t);
	const { bank, state, made } = rig({ master: master(120, 3.3) });
	await bank.toggle(slot('a'));
	const [{ at, fresh }] = made[0].starts;
	assert.ok(Math.abs(at - (10 + 0.7)) < QUANTUM_SEC, `start ${at} is not the downbeat at 10.7`);
	assert.equal(fresh, true, 'an await sat between the launch time and start()');
	assert.equal(state.voices[0].phase, 'armed');
	assert.equal(state.voices[0].free, false);
});

test('free run without a master', async (t) => {
	/** [if] no master plays [then] the voice starts after the lead at rate 1 and reports free, [else stop]. */
	timers(t);
	const { bank, state, made } = rig();
	await bank.toggle(slot('a'));
	assert.equal(made[0].starts[0].at, 10 + 0.05);
	assert.equal(state.voices[0].rate, 1);
	assert.equal(state.voices[0].free, true);
});

test('fifth trigger steals the oldest', async (t) => {
	/** [if] four voices sound and a fifth slot fires [then] the oldest voice is stopped and reused, [else stop]. */
	timers(t);
	const { bank, state, made } = rig({ master: master() });
	for (const id of ['a', 'b', 'c', 'd', 'e']) await bank.toggle(slot(id));
	assert.equal(made[0].stops, 1);
	assert.deepEqual(
		state.voices.map((v) => v.slot_id),
		['e', 'b', 'c', 'd']
	);
	assert.equal(bank.phase('a'), 'free');
});

test('second trigger stops', async (t) => {
	/** [if] a sounding slot fires again [then] its voice stops and the slot reads free, [else stop]. */
	timers(t);
	const { bank, state, made } = rig({ master: master() });
	await bank.toggle(slot('a'));
	await bank.toggle(slot('a'));
	assert.equal(made.length, 1);
	assert.equal(made[0].stops, 1);
	assert.equal(bank.phase('a'), 'free');
	assert.deepEqual(state.voices[0], B.idleVoiceState(5));
});

test('second press while loading cancels', async (t) => {
	/** [if] a slot fires twice before its clip loads [then] no voice is made, [else stop]. */
	timers(t);
	const { bank, made } = rig({ master: master() });
	await Promise.all([bank.toggle(slot('a')), bank.toggle(slot('a'))]);
	assert.equal(made.length, 0);
	assert.equal(bank.phase('a'), 'free');
});

test('armed turns playing on the tick', async (t) => {
	/** [if] the clock passes the launch time [then] the next 10 Hz tick marks the voice playing, [else stop]. */
	timers(t);
	const { bank, clock } = rig({ master: master() });
	await bank.toggle(slot('a'));
	clock.now = 10.7;
	t.mock.timers.tick(B.FOLLOW_TICK_MS);
	assert.equal(bank.phase('a'), 'playing');
	clock.now = 10.7 + 1;
	assert.equal(bank.playhead('a'), 0.5);
});

test('one shot stops after one pass', async (t) => {
	/** [if] a one-shot slot launches [then] its voice stops once the region has played, [else stop]. */
	timers(t);
	const { bank, made } = rig();
	await bank.toggle(slot('a', { mode: 'one_shot' }));
	t.mock.timers.tick(50 + 2000 + 1);
	assert.equal(made[0].stops, 1);
	assert.equal(bank.phase('a'), 'free');
});

test('preview on the cue bus', async (t) => {
	/** [if] a slot previews [then] it uses the cue bus and clock, takes no master voice, [else stop]. */
	timers(t);
	const { bank, state, made } = rig({ master: master() });
	await bank.preview(slot('a'));
	assert.equal(made[0].bus, 'cue');
	assert.equal(made[0].id, 'preview');
	assert.ok(Math.abs(made[0].starts[0].at - (10.7 + 0.25)) < QUANTUM_SEC);
	assert.ok(state.voices.every((v) => v.slot_id === null));
	assert.equal(bank.phase('a'), 'previewing');
	bank.stopPreview();
	assert.equal(made[0].stops, 1);
	assert.equal(bank.phase('a'), 'free');
});

test('master follow', async (t) => {
	/** [if] the master moves from 120 to 126 BPM [then] a matched voice follows at rate 1.05 on the tick, [else stop]. */
	timers(t);
	const { bank, state, clock, made } = rig({ master: master(120) });
	await bank.toggle(slot('a'));
	clock.master = master(126);
	t.mock.timers.tick(B.FOLLOW_TICK_MS);
	assert.equal(made[0].rates.length, 1);
	assert.ok(Math.abs(state.voices[0].rate - 1.05) < 1e-9);
	assert.ok(Math.abs(state.voices[0].pass_sec - 2 / 1.05) < 1e-9);
});

test('a master seek re-phases a sounding loop', async (t) => {
	/** [if] the master seeks while a loop sounds [then] the bank re-phases it at the next beat inside its loop, [else stop]. */
	timers(t);
	const { bank, clock, made } = rig({ master: master(120, 3.3) });
	await bank.toggle(slot('a'));
	clock.now = 11;
	bank.followTick();
	assert.equal(made[0].rephases.length, 0, 'an in-phase voice re-phased');
	clock.master = master(120, 3.3 + 0.37);
	clock.now = 11.1;
	bank.followTick();
	assert.equal(made[0].rephases.length, 1);
	const [at, clipSec] = made[0].rephases[0];
	// master at 11.1 reads 4.77 s; its next beat (5.0 s) is 0.23 s away; the voice is 1.26 beats in
	assert.ok(Math.abs(at - 11.33) < QUANTUM_SEC, `re-phase at ${at}`);
	assert.ok(Math.abs(clipSec - (made[0].plan.guardSec + 0.5)) < 1e-9, `clip ${clipSec}`);
	assert.equal(B.locksToMaster(slot('g', { mode: 'gate' }).settings, false, true), false);
	assert.equal(B.locksToMaster(slot('o').settings, false, false), false);
});

test('followMaster rules', () => {
	/** [if] tempo is own, no master plays, or the rate holds [then] no follow happens, [else stop]. */
	const m = { bpm: 126, key: '8A' };
	assert.equal(B.followMaster(slot('a', { tempo: 'own' }), 1, m), null);
	assert.equal(B.followMaster(slot('a'), 1, null), null);
	assert.equal(B.followMaster(slot('a'), 1.05, m), null);
	assert.ok(Math.abs(B.followMaster(slot('a'), 1, m).rate - 1.05) < 1e-9);
});

test('chooseVoice', () => {
	/** [if] a free voice holds the clip it wins; all busy steals the oldest; all loading throws, [else stop]. */
	const none = new Map();
	assert.deepEqual(
		B.chooseVoice(
			() => false,
			(id) => id === 7,
			none
		),
		{ voice: 7, steal: false }
	);
	const ages = new Map([
		[5, 4],
		[6, 2],
		[7, 9],
		[8, 3]
	]);
	assert.deepEqual(
		B.chooseVoice(
			() => true,
			() => false,
			ages
		),
		{ voice: 6, steal: true }
	);
	assert.throws(
		() =>
			B.chooseVoice(
				() => true,
				() => false,
				none
			),
		/still loading/
	);
});

test('gate launches on press', () => {
	/** [if] the slot is gate mode with a master [then] it launches after the lead, unquantised, [else stop]. */
	assert.deepEqual(B.launchFor(10, master(), { launch: 'bar', downbeat_offset: 0, mode: 'gate' }), {
		at: 10.05,
		free: false
	});
});

test('stopAll silences voices and preview', async (t) => {
	/** [if] two voices sound and one slot previews [then] stopAll stops all three and frees every voice, [else stop]. */
	timers(t);
	const { bank, state, made } = rig({ master: master() });
	await bank.toggle(slot('a'));
	await bank.toggle(slot('b'));
	await bank.preview(slot('c'));
	bank.stopAll();
	assert.deepEqual(
		made.map((v) => v.stops),
		[1, 1, 1]
	);
	assert.ok(state.voices.every((v) => v.slot_id === null) && state.preview === null);
});
