/**
 * Closed-loop phase-lock sim on REAL grids: sim.mjs <frontend dir> <pair-beats.json> <masterStartSec> <followerStartSec> <blendSec> [pitchRange] [keyLocked]
 * Drives the shipped phase-lock.ts + computeFollowerSyncPlan the way both engines' ticks do (30 Hz).
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [frontendArg, beatsArg, mStart, fStart, blend, range = '16', keyLockedArg = '1'] = process.argv.slice(2);
// Resolve every path argument against the caller's directory BEFORE chdir, so relative paths work.
const frontend = resolve(frontendArg);
const beatsPath = resolve(beatsArg);
process.chdir(frontend);
const { loadTypeScriptModule } = await import(pathToFileURL(`${frontend}/tests/unit/load-typescript.mjs`).href);
const bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
const pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
const pair = JSON.parse(readFileSync(beatsPath, 'utf8'));
const masterGrid = pair.out;
const followerGrid = pair.in;
const pitchRangePct = Number(range);
const keyLocked = keyLockedArg === '1';
const tick = 1 / 30;
let m = Number(mStart);
let f = Number(fStart);
let base = 1;
let normalization = 1;
const rejoin = () => {
	const p = bsm.computeFollowerSyncPlan({
		masterGrid, followerGrid, masterPositionAtSyncSec: m, masterTempoRatio: 1, followerPositionSec: f,
		currentContextTimeSec: 0, syncAtContextTimeSec: 0.01,
		minFollowerTempoRatio: 1 - pitchRangePct / 100, maxFollowerTempoRatio: 1 + pitchRangePct / 100, mode: 'beat'
	});
	f = p.followerPositionSec;
	base = p.followerTempoRatio;
	normalization = p.tempoNormalization;
};
rejoin();
const joinRatio = base;
let sent = base;
let sinceJoinSec = 0;
let overLineTicks = 0;
let reseeks = 0;
let seekJumpSec = 0;
const errs = [];
const late = [];
const LATE_SEC = Number(process.env.LATE_SEC ?? '1e9');
let flamSec = 0;
let unmeasured = 0;
for (let k = 0; k < Number(blend) * 30; k++) {
	const input = {
		masterBeats: masterGrid, masterPositionSec: m, masterTempo: 1, followerBeats: followerGrid,
		followerPositionSec: f, followerBaseTempo: base, normalization, pitchRangePct, keyLocked, sinceJoinSec, overLineTicks
	};
	base = pl.phaseLockFeedForwardBase(input);
	const d = pl.phaseLockDecision({ ...input, followerBaseTempo: base, trimming: sent !== base });
	base = d.base;
	overLineTicks = d.overLineTicks;
	sinceJoinSec += tick;
	if (d.action === 'unmeasured') unmeasured += 1;
	else {
		errs.push(Math.abs(d.errorMs));
		if (f >= LATE_SEC) late.push(Math.abs(d.errorMs));
		if (Math.abs(d.errorMs) >= 10) flamSec += tick;
	}
	if (d.action === 'reseek') {
		reseeks += 1;
		const before = f;
		rejoin();
		seekJumpSec += Math.abs(f - before);
		if (process.env.TRACE) console.log("reseek", m.toFixed(2), before.toFixed(2), "->", f.toFixed(2), "base", base.toFixed(4), "err", d.errorMs.toFixed(1));
		sent = base;
		sinceJoinSec = 0;
		overLineTicks = 0;
	} else if (pl.phaseLockShouldSend(sent, d.tempo, base)) sent = d.tempo;
	m += tick;
	f += tick * sent;
}
const sorted = [...errs].sort((a, b) => a - b);
const lateSorted = [...late].sort((a, b) => a - b);
const pct = (p) => sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * p))];
console.log(JSON.stringify({
	joinRatio: +joinRatio.toFixed(4), normalization, reseeks, seekJumpSec: +seekJumpSec.toFixed(2),
	p50Ms: +pct(0.5).toFixed(1), p95Ms: +pct(0.95).toFixed(1), maxMs: +sorted.at(-1).toFixed(1),
	flamSec: +flamSec.toFixed(1), lateSec: +(late.length / 30).toFixed(1), lateP95Ms: late.length ? +lateSorted[Math.floor((late.length - 1) * 0.95)].toFixed(1) : null, lateP50Ms: late.length ? +lateSorted[Math.floor((late.length - 1) * 0.5)].toFixed(1) : null, measuredSec: +(errs.length / 30).toFixed(1), unmeasuredTicks: unmeasured, endFollowerSec: +f.toFixed(1)
}));
