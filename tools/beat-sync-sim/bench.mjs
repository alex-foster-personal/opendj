// Cost of one phaseLockDecision tick: plain, and on the out-of-reach re-join probe path. bench.mjs <frontend> <beats.json>
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [frontendArg, beatsArg] = process.argv.slice(2);
// Resolve every path argument against the caller's directory BEFORE chdir, so relative paths work.
const frontend = resolve(frontendArg);
const beatsPath = resolve(beatsArg);
process.chdir(frontend);
const { loadTypeScriptModule } = await import(pathToFileURL(`${frontend}/tests/unit/load-typescript.mjs`).href);
const pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
const all = JSON.parse(readFileSync(beatsPath, 'utf8'));
const input = (followerPositionSec, overLineTicks, sinceJoinSec) => ({
	masterBeats: all.out, masterPositionSec: 230.1, masterTempo: 1, followerBeats: all.in, followerPositionSec,
	followerBaseTempo: 1.14, normalization: 2, pitchRangePct: 16, keyLocked: false, sinceJoinSec, overLineTicks
});
function time(label, make, n) {
	for (let i = 0; i < 50; i++) pl.phaseLockDecision(make(i));
	const t0 = process.hrtime.bigint();
	for (let i = 0; i < n; i++) pl.phaseLockDecision(make(i));
	const us = Number(process.hrtime.bigint() - t0) / 1000 / n;
	console.log(label, `${us.toFixed(1)} us/decision`);
}
time('in reach, under the line', (i) => input(70 + (i % 100) * 0.01, 0, 1), 20000);
time('out of reach, re-join probe', (i) => input(30 + (i % 100) * 0.01, 5, 20), 2000);
