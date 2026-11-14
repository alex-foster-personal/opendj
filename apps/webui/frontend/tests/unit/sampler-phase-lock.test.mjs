/**
 * Issue #10781 (S4): phase lock of a sounding sampler voice to the master (spec 4.3) and the loop
 * seam click check (spec 4.4, section 9 Q2). The voice in each simulation is counted by the test
 * itself from the rates and re-phases the lock sends, against the master's grid position.
 *
 * Regression lines:
 * - if a voice plays 64 bars against a steady master then every tick reads under 1 ms and the trim stays within 0.5 percent
 * - if the master BPM disagrees with its grid by 1 percent then the trim stops at 0.5 percent and the voice re-phases
 * - if the master tempo steps then the voice follows on the next tick and holds without a re-phase
 * - if the master seeks, loops, jogs or changes deck then the voice re-phases on the master's next beat
 * - if a loop is whole cycles then the seam does not click; a mid-cycle cut does
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let L;
before(async () => {
	L = await loadTypeScriptModule('src/lib/player/sampler/sampler-phase-lock.ts');
});

const TICK = 0.1;
const NATIVE_BPM = 120;
const VOICE_BEAT_SEC = 60 / NATIVE_BPM;
/** 64 bars at 128 BPM, plus margin. */
const BARS_64_SEC = (64 * 4 * 60) / 128;

const grid = (bpm, n = 600) =>
	Array.from({ length: n }, (_, i) => ({ n: (i % 4) + 1, t: (i * 60) / bpm }));

/**
 * A master that plays `bpm` (grid) at `ratio` from track `p0` when the clock reads 0, with
 * events `[ctx, {seek?, ratio?, deck?}]` applied from that clock time on.
 */
function masterModel({ bpm = 128, ratio = 1, p0 = 4 * (60 / 128) * 2, events = [] } = {}) {
	const beats = grid(bpm);
	return (now) => {
		let pos = p0;
		let r = ratio;
		let deck = 1;
		let from = 0;
		for (const [at, e] of events) {
			if (at > now) break;
			pos += (at - from) * r;
			from = at;
			if (e.seek !== undefined) pos += e.seek;
			if (e.ratio !== undefined) r = e.ratio;
			if (e.deck !== undefined) deck = e.deck;
		}
		return { deck, beats, positionSec: pos + (now - from) * r, tempoRatio: r };
	};
}

/** Runs the lock for `seconds` of ticks; `baseAt(now)` is the tempo-matched rate the bank feeds. */
function simulate(masterAt, baseAt, seconds, startCtx = 0.25) {
	const m0 = masterAt(startCtx);
	// launch on the master's next beat, as the bank's launch quantise does
	const beat0 = L.masterBeatAt(m0.beats, m0.positionSec);
	const at = startCtx + ((Math.ceil(beat0) - beat0) * (60 / 128)) / m0.tempoRatio;
	let s = L.startPhaseLock(at, baseAt(at), VOICE_BEAT_SEC, masterAt(at));
	let rate = baseAt(at);
	let voiceBeats = 0;
	let pending = null;
	let last = at;
	const ticks = [];
	const rephases = [];
	for (let now = at + TICK; now < at + seconds; now += TICK) {
		voiceBeats += ((now - last) * rate) / VOICE_BEAT_SEC;
		if (pending !== null && now >= pending.at) {
			voiceBeats = pending.voiceBeat + ((now - pending.at) * rate) / VOICE_BEAT_SEC;
			pending = null;
		}
		last = now;
		const m = masterAt(now);
		const masterBeat = L.masterBeatAt(m.beats, m.positionSec);
		const errBeats = voiceBeats / s.k - masterBeat;
		const trueErrMs = ((errBeats - Math.round(errBeats)) / L.masterBeatsPerSec(m)) * 1000;
		const step = L.phaseLockTick(s, now, baseAt(now), m);
		s = step.state;
		if (step.rate !== null) rate = step.rate;
		if (step.rephase !== null) {
			pending = step.rephase;
			rephases.push({
				now,
				...step.rephase,
				masterBeatAtAt: L.masterBeatAt(m.beats, masterAt(step.rephase.at).positionSec)
			});
		}
		ticks.push({ now, trueErrMs, trim: rate / baseAt(now) - 1, sent: step.rate });
	}
	return { ticks, rephases };
}

const maxAbs = (xs) => Math.max(...xs.map(Math.abs));
const steadyBase = (bpm) => () => bpm / NATIVE_BPM;

test('64 bars against a steady master', () => {
	/** [if] a voice plays 64 bars against a steady 128 BPM master [then] every tick is under 1 ms, trim within 0.5%, [else stop]. */
	const { ticks, rephases } = simulate(masterModel(), steadyBase(128), BARS_64_SEC);
	assert.ok(ticks.length >= 1190, `only ${ticks.length} ticks`);
	assert.ok(maxAbs(ticks.map((t) => t.trueErrMs)) < 1, 'phase error reached 1 ms');
	assert.ok(maxAbs(ticks.map((t) => t.trim)) <= L.PHASE_LOCK_MAX_TRIM + 1e-12);
	assert.equal(rephases.length, 0);
});

test('a small BPM disagreement is held by the bounded trim', () => {
	/** [if] the master BPM reads 0.2% off its grid [then] the trim holds the error at R x 4 beats, no re-phase, [else stop]. */
	const { ticks, rephases } = simulate(masterModel(), steadyBase(128 * 1.002), BARS_64_SEC);
	assert.equal(rephases.length, 0);
	// a proportional lock settles a rate error R at R times its window (the deck lock's law)
	const settleMs = 0.002 * L.PHASE_LOCK_CORRECTION_BEATS * (60 / 128) * 1000;
	assert.ok(maxAbs(ticks.map((t) => t.trueErrMs)) < settleMs + 0.5, 'error ran away');
	assert.ok(maxAbs(ticks.map((t) => t.trim)) <= L.PHASE_LOCK_MAX_TRIM + 1e-12);
	assert.ok(
		ticks.some((t) => t.trim < 0),
		'the lock never trimmed'
	);
});

test('the trim never passes 0.5 percent', () => {
	/** [if] the master BPM reads 1% off its grid [then] the trim stops at 0.5% and the voice re-phases instead, [else stop]. */
	const { ticks, rephases } = simulate(masterModel(), steadyBase(128 * 1.01), 30);
	const worst = maxAbs(ticks.map((t) => t.trim));
	assert.ok(worst <= L.PHASE_LOCK_MAX_TRIM + 1e-12, `trim ${worst}`);
	assert.ok(Math.abs(worst - L.PHASE_LOCK_MAX_TRIM) < 1e-9, 'the trim never reached its cap');
	assert.ok(rephases.length > 0, 'a runaway error never re-phased');
});

test('a master tempo step is followed on the next tick', () => {
	/** [if] the master steps 128 to 132 BPM [then] the rate follows on the next tick and holds under 4 ms, no re-phase, [else stop]. */
	const stepAt = 10.03;
	const masterAt = masterModel({ events: [[stepAt, { ratio: 132 / 128 }]] });
	// the bank reads the new BPM on its first tick after the step (followMaster)
	const ticksAfter = (now) => now >= stepAt + 1e-9;
	const baseAt = (now) => (ticksAfter(now) ? 132 : 128) / NATIVE_BPM;
	const { ticks, rephases } = simulate(masterAt, baseAt, 30);
	assert.equal(rephases.length, 0);
	const first = ticks.find((t) => t.now >= stepAt);
	assert.ok(first.now - stepAt <= 0.1 + 1e-9, 'followed later than 100 ms');
	assert.ok(
		first.sent !== null && Math.abs(first.sent / (132 / NATIVE_BPM) - 1) <= L.PHASE_LOCK_MAX_TRIM
	);
	assert.ok(maxAbs(ticks.map((t) => t.trueErrMs)) < 4);
	assert.ok(
		maxAbs(ticks.filter((t) => t.now > stepAt + 4).map((t) => t.trueErrMs)) <
			L.PHASE_LOCK_DEADBAND_MS
	);
});

/** Asserts the last re-phase came after the event, landed on a master beat, and left the voice under 1 ms. */
function assertRephased({ ticks, rephases }, afterCtx) {
	const r = rephases.at(-1);
	assert.ok(r !== undefined && r.now >= afterCtx, 'no re-phase after the event');
	assert.ok(r.at >= r.now + 0.05 - 1e-9, 're-phase scheduled inside the launch lead');
	assert.ok(
		Math.abs(r.masterBeatAtAt - Math.round(r.masterBeatAtAt)) < 1e-6,
		're-phase is off the beat'
	);
	const settled = ticks.filter((t) => t.now > r.at + 1e-9);
	assert.ok(maxAbs(settled.map((t) => t.trueErrMs)) < 1, 'still out of phase after the re-phase');
}

test('a master seek re-phases at the next beat', () => {
	/** [if] the master seeks 1.37 beats [then] the voice re-phases on the master's next beat and is under 1 ms, [else stop]. */
	const masterAt = masterModel({ events: [[20.02, { seek: 1.37 * (60 / 128) }]] });
	assertRephased(simulate(masterAt, steadyBase(128), 40), 20.02);
});

test('a master loop re-phases at the next beat', () => {
	/** [if] the master loops 4 beats back [then] the voice re-phases at the next beat and stays under 1 ms, [else stop]. */
	const masterAt = masterModel({ events: [[15.04, { seek: -4 * (60 / 128) }]] });
	assertRephased(simulate(masterAt, steadyBase(128), 30), 15.04);
});

test('a master jog re-phases once the error passes the line', () => {
	/** [if] the master is jogged 3 ms a tick for six ticks [then] the voice re-phases past 15 ms, then under 1 ms, [else stop]. */
	const events = Array.from({ length: 6 }, (_, i) => [12.05 + i * TICK, { seek: 0.003 }]);
	assertRephased(simulate(masterModel({ events }), steadyBase(128), 30), 12.05);
});

test('a change of master re-phases', () => {
	/** [if] another deck becomes master [then] the voice re-phases on its next beat, [else stop]. */
	const masterAt = masterModel({ events: [[18.01, { deck: 2, seek: 0.21 }]] });
	assertRephased(simulate(masterAt, steadyBase(128), 30), 18.01);
});

test('no master holds the rate', () => {
	/** [if] the master stops [then] the lock sends nothing; when it returns the voice re-phases, [else stop]. */
	const masterAt = masterModel();
	let s = L.startPhaseLock(1, 128 / NATIVE_BPM, VOICE_BEAT_SEC, masterAt(1));
	const gone = L.phaseLockTick(s, 1.1, 128 / NATIVE_BPM, null);
	assert.equal(gone.rate, null);
	assert.equal(gone.rephase, null);
	s = gone.state;
	const back = L.phaseLockTick(s, 1.2, 128 / NATIVE_BPM, masterAt(1.2));
	assert.ok(back.rephase !== null);
});

test('half time counts master beats', () => {
	/** [if] a 64 BPM slot plays against 128 (k = 2) [then] the lock counts two voice beats per master beat, [else stop]. */
	const m = masterModel()(0);
	const s = L.startPhaseLock(0, 1, 60 / 256, m);
	assert.equal(s.k, 2);
});

test('trim and grid helpers', () => {
	/** [if] the error is large the trim is capped, the grid index is fractional, the clip wraps in the loop, [else stop]. */
	assert.equal(L.trimFor(1), -L.PHASE_LOCK_MAX_TRIM);
	assert.equal(L.trimFor(-1), L.PHASE_LOCK_MAX_TRIM);
	assert.ok(Math.abs(L.trimFor(0.004) + 0.001) < 1e-12);
	assert.ok(Math.abs(L.masterBeatAt(grid(120), 1.25) - 2.5) < 1e-12);
	const plan = { guardSec: 0.25, loop: true, loopStartSec: 0, loopEndSec: 2 };
	assert.ok(Math.abs(L.rephaseClipSec(plan, 5, 0.5) - 0.75) < 1e-12);
	assert.equal(L.rephaseClipSec({ ...plan, loop: false }, 5, 0.5), null);
});

//----- seam

const SR = 48_000;
const sine = (hz, sec, amp = 0.9) =>
	Float32Array.from(
		{ length: Math.round(sec * SR) },
		(_, n) => amp * Math.sin((2 * Math.PI * hz * n) / SR)
	);

test('a whole-cycle loop seam does not click', () => {
	/** [if] the loop is whole cycles of a 480 Hz sine [then] no step above 0.1 FS within 5 ms of the seam, [else stop]. */
	const ch = sine(480, 2);
	assert.equal(L.seamClicks([ch, ch], SR, 0, 1), false);
	assert.ok(L.seamStepFs([ch], SR, 0, 1) < L.SEAM_CLICK_FS);
});

test('a mid-cycle cut clicks', () => {
	/** [if] a loud loop is cut a quarter cycle in [then] the seam reads as a click, [else stop]. */
	const ch = sine(480, 2);
	assert.equal(L.seamClicks([ch], SR, 0, 1 + 25 / SR), true);
	assert.throws(() => L.seamStepFs([ch], SR, 0, 0.004), /shorter than the seam window/);
});
