/**
 * Before/after phase-lock evaluation on REAL rekordbox grids (closed loop, 30 Hz, both engines' tick).
 * evalpairs.mjs <frontend dir> <beats.json> <out.json>
 * Master = key "out" (Monster) from its bar 85 for 32 bars; follower = every other key, joined at its bar N downbeat.
 * "before" = phase-lock.ts at origin/main (copied to zz-phase-lock-before.ts), "after" = the working tree.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [frontendArg, beatsArg, outArg] = process.argv.slice(2);
// Resolve every path argument against the caller's directory BEFORE chdir, so relative paths work.
const frontend = resolve(frontendArg);
const beatsPath = resolve(beatsArg);
const outPath = resolve(outArg);
process.chdir(frontend);
const { loadTypeScriptModule } = await import(pathToFileURL(`${frontend}/tests/unit/load-typescript.mjs`).href);
const bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
const libs = {
	before: await loadTypeScriptModule('src/lib/rb/zz-phase-lock-before.ts'),
	after: await loadTypeScriptModule('src/lib/rb/phase-lock.ts')
};
const all = JSON.parse(readFileSync(beatsPath, 'utf8'));
const masterGrid = all.out;
const downbeat = (grid, bar) => grid.filter((b) => b.n === 1)[bar - 1].t;
const MASTER_START = downbeat(masterGrid, 85);
const BLEND_SEC = (32 * 4 * 60) / 93.75;
const RANGE = 16;
const tick = 1 / 30;

function run(pl, followerGrid, followerStart, keyLocked) {
	let m = MASTER_START;
	let f = followerStart;
	let base = 1;
	let normalization = 1;
	const rejoin = () => {
		const p = bsm.computeFollowerSyncPlan({
			masterGrid, followerGrid, masterPositionAtSyncSec: m, masterTempoRatio: 1, followerPositionSec: f,
			currentContextTimeSec: 0, syncAtContextTimeSec: 0.01,
			minFollowerTempoRatio: 1 - RANGE / 100, maxFollowerTempoRatio: 1 + RANGE / 100, mode: 'beat'
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
	let backSec = 0;
	let flamSec = 0;
	const errs = [];
	const traj = [];
	for (let k = 0; k < BLEND_SEC * 30; k++) {
		const input = {
			masterBeats: masterGrid, masterPositionSec: m, masterTempo: 1, followerBeats: followerGrid,
			followerPositionSec: f, followerBaseTempo: base, normalization, pitchRangePct: RANGE, keyLocked, sinceJoinSec, overLineTicks
		};
		base = pl.phaseLockFeedForwardBase(input);
		const d = pl.phaseLockDecision({ ...input, followerBaseTempo: base, trimming: sent !== base });
		base = d.base;
		overLineTicks = d.overLineTicks;
		sinceJoinSec += tick;
		if (d.action !== 'unmeasured') {
			errs.push(Math.abs(d.errorMs));
			if (Math.abs(d.errorMs) >= 10) flamSec += tick;
		}
		if (d.action === 'reseek') {
			reseeks += 1;
			const was = f;
			rejoin();
			if (f < was) backSec += was - f;
			sent = base;
			sinceJoinSec = 0;
			overLineTicks = 0;
		} else if (pl.phaseLockShouldSend(sent, d.tempo, base)) sent = d.tempo;
		traj.push([+f.toFixed(5), +sent.toFixed(6), d.errorMs === null ? null : +d.errorMs.toFixed(2)]);
		m += tick;
		f += tick * sent;
	}
	const s = [...errs].sort((a, b) => a - b);
	const q = (p) => +s[Math.floor((s.length - 1) * p)].toFixed(1);
	return { joinRatio: +joinRatio.toFixed(4), normalization, reseeks, backSec: +backSec.toFixed(1), p50Ms: q(0.5), p95Ms: q(0.95), flamSec: +flamSec.toFixed(1), progressSec: +(f - followerStart).toFixed(1), traj };
}

const START_BARS = { in: 4, november: 1, roxanne: 1, randy: 1, imma: 1, fourth: 1 };
const rows = [];
for (const [name, grid] of Object.entries(all)) {
	if (name === 'out') continue;
	const start = downbeat(grid, START_BARS[name] ?? 1);
	for (const keyLocked of [false, true]) {
		const row = { track: name, keyLocked, startSec: start };
		for (const [label, pl] of Object.entries(libs)) {
			try {
				row[label] = run(pl, grid, start, keyLocked);
			} catch (e) {
				row[label] = { error: String(e.message ?? e) };
			}
		}
		rows.push(row);
		
	}
}
writeFileSync(outPath, JSON.stringify(rows, null, 1));
