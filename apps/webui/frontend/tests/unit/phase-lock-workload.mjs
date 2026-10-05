/**
 * Phase-lock workload simulation: what the continuous Beat Sync lock DOES to a
 * follower on a given grid, counted per minute of locked play.
 *
 * This is the simulation ADR deck-beatgrid-source Amendment 1 measured with
 * (`lock_workload` in scripts/beatbench/straighten_eval.py on
 * af--adr-beatgrid-source), ported so it drives the SHIPPED decision instead
 * of a Python copy of it: the follower on the grid under test, against an
 * ideal even master at that grid's mean tempo, 30 ticks a second, an instant
 * re-join that lines up the STORED beats (which is what the join does).
 * `legacyPhaseLockDecision` is the lock as it was before it read uneven grids
 * smoothed, kept as the "before" instrument so both columns come from one
 * simulation on one set of grids.
 *
 * Requirements:
 *   ✔︎ ✅ 🎯 Same model as the ADR's, so before/after is attributable.
 *     [if] the legacy decision on the ADR's grids does not reproduce the ADR's
 *     published median (82.6 re-joins a minute) [then ⛔️] (checked by the live
 *     runner, tests/live/phase-lock-jitter-workload.mjs)
 *     [if] an even grid shows any re-join or trim under either decision [then ⛔️]
 *   ✔︎ ✅ 🎯 Fixtures are synthetic: no library track is named or stored.
 *
 * Used by phase-lock-jitter.test.mjs and tests/live/phase-lock-jitter-workload.mjs.
 */

export const WORKLOAD_TICK_SEC = 1 / 30;
export const WORKLOAD_PITCH_RANGE_PCT = 8;

// ------------------------------------------------------------ small stats

export function median(values) {
	if (values.length === 0) throw new RangeError('median of nothing');
	const sorted = [...values].sort((a, b) => a - b);
	const mid = sorted.length >> 1;
	return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export function percentile(values, pct) {
	if (values.length === 0) throw new RangeError('percentile of nothing');
	const sorted = [...values].sort((a, b) => a - b);
	const at = ((sorted.length - 1) * pct) / 100;
	const lo = Math.floor(at);
	const hi = Math.min(lo + 1, sorted.length - 1);
	return sorted[lo] + (sorted[hi] - sorted[lo]) * (at - lo);
}

// ------------------------------------------------------- synthetic grids

/** Deterministic uniform [0, 1). */
export function lcg(seed) {
	let state = seed >>> 0;
	return () => {
		state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
		return state / 4294967296;
	};
}

function _beats(times) {
	return times.map((t, i) => ({ n: (i % 4) + 1, bpm: 0, t }));
}

/**
 * Beat times for a tempo curve: `bpmAt(i)` is the tempo of interval i.
 * Returns the TRUE times; `storedGrid` turns them into what an analyzer stores.
 */
export function trueBeatTimes({ beats, startSec = 0.2, bpmAt }) {
	const times = [startSec];
	for (let i = 0; i < beats - 1; i++) times.push(times[i] + 60 / bpmAt(i));
	return times;
}

/**
 * What gets stored for `trueTimes`: each beat moved by up to `jitterMs` (the
 * analyzer following where a hit landed), a share `strayShare` of them by up
 * to `strayMs` instead (a beat placed on the wrong transient), and then
 * rounded to `stepMs`. stepMs 1 with no jitter is an ordinary even grid in
 * millisecond storage; stepMs 5 or 10 is a rekordbox dynamic grid, whose
 * intervals dither between neighboring steps (480, 490, 480 ...) around the
 * real tempo. The shapes come from the 19 uneven grids the ADR measured
 * (interval histograms in 5 and 10 ms steps; half the beats within 2 to 9 ms
 * of a smooth line, one in twenty 15 to 50 ms off it), not from any one track.
 */
export function storedGrid(trueTimes, { stepMs, jitterMs = 0, strayShare = 0, strayMs = 0, seed = 1 }) {
	const random = lcg(seed);
	const step = stepMs / 1000;
	return _beats(
		trueTimes.map((t) => {
			const reachMs = random() < strayShare ? strayMs : jitterMs;
			return Math.round((t + ((random() * 2 - 1) * reachMs) / 1000) / step) * step;
		})
	);
}

/** An ideal even grid of `count` beats, `periodSec` apart, from `startSec`. */
export function evenGrid(periodSec, count, startSec = 0) {
	return _beats(Array.from({ length: count }, (_, i) => startSec + i * periodSec));
}

// ------------------------------------------------- the lock as it was

const LEGACY = { deadbandMs: 2, releaseMs: 1, correctionBeats: 4, maxTrim: 0.003, reseekBeats: 0.25, reseekMs: 15 };

function _position(beats, positionSec) {
	const n = beats.length;
	if (n < 2 || positionSec < beats[0].t || positionSec >= beats[n - 1].t) return null;
	let lo = 0;
	let hi = n - 1;
	while (hi - lo > 1) {
		const mid = (lo + hi) >> 1;
		if (beats[mid].t <= positionSec) lo = mid;
		else hi = mid;
	}
	return lo + (positionSec - beats[lo].t) / (beats[lo + 1].t - beats[lo].t);
}

/** Phase error read through the STORED beats, each taken as truth. */
export function storedPhaseErrorMs(input) {
	const m = _position(input.masterBeats, input.masterPositionSec);
	const f = _position(input.followerBeats, input.followerPositionSec);
	if (m === null || f === null) return null;
	const mi = Math.floor(m);
	const beatWallMs = ((input.masterBeats[mi + 1].t - input.masterBeats[mi].t) / input.masterTempo) * 1000;
	const normalization = input.normalization ?? 1;
	const unit = Math.min(1, 1 / normalization);
	const diff = f / normalization - m;
	return (diff - unit * Math.round(diff / unit)) * beatWallMs;
}

/** `phaseLockDecision` before this change (PR 4602 as cherry-picked), same input. */
export function legacyPhaseLockDecision(input) {
	const base = input.followerBaseTempo;
	const errorMs = storedPhaseErrorMs(input);
	if (errorMs === null) return { action: 'unmeasured', tempo: base, errorMs: null, base, overLineTicks: 0 };
	const mi = Math.floor(_position(input.masterBeats, input.masterPositionSec));
	const beatWallMs = ((input.masterBeats[mi + 1].t - input.masterBeats[mi].t) / input.masterTempo) * 1000;
	if (Math.abs(errorMs) > Math.min(LEGACY.reseekBeats * beatWallMs, LEGACY.reseekMs)) {
		return { action: 'reseek', tempo: base, errorMs, base, overLineTicks: 0 };
	}
	if (Math.abs(errorMs) < (input.trimming ? LEGACY.releaseMs : LEGACY.deadbandMs)) {
		return { action: 'base', tempo: base, errorMs, base, overLineTicks: 0 };
	}
	const correction = -errorMs / (LEGACY.correctionBeats * beatWallMs);
	const trim = Math.max(-LEGACY.maxTrim, Math.min(LEGACY.maxTrim, correction));
	const range = input.pitchRangePct / 100;
	const tempo = Math.max(Math.max(0.01, 1 - range), Math.min(1 + range, base * (1 + trim)));
	return { action: 'trim', tempo, errorMs, base, overLineTicks: 0 };
}

// ---------------------------------------------------------- the simulation

/**
 * Play `followerBeats` from its first beat to its last under `decide`.
 *
 * `masterBeats` defaults to the ADR's master: ideal, even, at the follower
 * grid's MEAN interval (mean, not median: millisecond storage biases a median
 * interval by up to 0.1%, which would read as a tempo mismatch on an even
 * grid). `truthBeats`, when the fixture knows it, is the follower grid before
 * storage noise: the error read through it is how far the AUDIO is off.
 * `events` are `{ atSec, displaceMs, registered }`: the follower is moved by
 * `displaceMs` of wall clock at master time `atSec`, and the lock is told
 * (`userOffsetMs`) only when `registered`.
 */
export function lockWorkload({
	decide,
	followerBeats,
	masterBeats,
	truthBeats,
	events = [],
	maxTrim = 0.003,
	trace: keepTrace = false
}) {
	const n = followerBeats.length;
	const periodSec = (followerBeats[n - 1].t - followerBeats[0].t) / (n - 1);
	const master = masterBeats ?? evenGrid(periodSec, n + 8);
	const pending = [...events].sort((a, b) => a.atSec - b.atSec);
	let followerSec = followerBeats[0].t;
	let masterSec = master[0].t;
	let base = 1;
	let tempo = 1;
	let trimming = false;
	let overLineTicks = 0;
	let sinceJoinSec = 0;
	let userOffsetMs = 0;
	let ticks = 0;
	let trimTicks = 0;
	let capTicks = 0;
	let reseeks = 0;
	let baseMoves = 0;
	const storedAbsMs = [];
	const lockAbsMs = [];
	const truthAbsMs = [];
	const trace = [];
	for (;;) {
		while (pending.length > 0 && masterSec >= pending[0].atSec) {
			const event = pending.shift();
			followerSec += (event.displaceMs / 1000) * tempo;
			if (event.registered) userOffsetMs += event.displaceMs;
		}
		const input = {
			masterBeats: master,
			masterPositionSec: masterSec,
			masterTempo: 1,
			followerBeats,
			followerPositionSec: followerSec,
			followerBaseTempo: base,
			pitchRangePct: WORKLOAD_PITCH_RANGE_PCT,
			trimming,
			sinceJoinSec,
			overLineTicks,
			userOffsetMs
		};
		const stored = storedPhaseErrorMs(input);
		if (stored === null) break;
		ticks += 1;
		storedAbsMs.push(Math.abs(stored));
		const truth = truthBeats === undefined ? null : storedPhaseErrorMs({ ...input, followerBeats: truthBeats });
		if (truth !== null) truthAbsMs.push(Math.abs(truth));
		const decision = decide(input);
		if (decision.errorMs !== null) lockAbsMs.push(Math.abs(decision.errorMs));
		if (decision.base !== base) baseMoves += 1;
		base = decision.base;
		overLineTicks = decision.overLineTicks;
		if (keepTrace) {
			trace.push({
				masterSec,
				action: decision.action,
				tempo: decision.tempo,
				base,
				storedMs: stored,
				truthMs: truth,
				lockMs: decision.errorMs
			});
		}
		if (decision.action === 'reseek') {
			// The join: seek so the STORED beats line up, and re-pick the tempo.
			reseeks += 1;
			const m = _position(master, masterSec);
			const f = _position(followerBeats, followerSec);
			const diff = f - m;
			const target = f - (diff - Math.round(diff));
			const lo = Math.max(0, Math.min(Math.floor(target), n - 2));
			followerSec = followerBeats[lo].t + (target - lo) * (followerBeats[lo + 1].t - followerBeats[lo].t);
			base = 1;
			tempo = 1;
			trimming = false;
			overLineTicks = 0;
			sinceJoinSec = 0;
			userOffsetMs = 0;
		} else {
			tempo = decision.tempo;
			trimming = decision.action === 'trim';
			if (trimming) {
				trimTicks += 1;
				if (Math.abs(tempo / base - 1) >= maxTrim * (1 - 1e-9)) capTicks += 1;
			}
		}
		followerSec += WORKLOAD_TICK_SEC * tempo;
		masterSec += WORKLOAD_TICK_SEC;
		sinceJoinSec += WORKLOAD_TICK_SEC;
	}
	if (ticks === 0) throw new RangeError('lock workload: the follower grid gave no measurable tick');
	const minutes = (ticks * WORKLOAD_TICK_SEC) / 60;
	return {
		ticks,
		minutes,
		reseeks,
		reseeksPerMin: reseeks / minutes,
		trimTimeFrac: trimTicks / ticks,
		capTimeFrac: capTicks / ticks,
		baseMovesPerMin: baseMoves / minutes,
		medianStoredErrMs: median(storedAbsMs),
		p95StoredErrMs: percentile(storedAbsMs, 95),
		medianLockErrMs: median(lockAbsMs),
		medianTruthErrMs: truthAbsMs.length > 0 ? median(truthAbsMs) : null,
		p95TruthErrMs: truthAbsMs.length > 0 ? percentile(truthAbsMs, 95) : null,
		trace
	};
}
