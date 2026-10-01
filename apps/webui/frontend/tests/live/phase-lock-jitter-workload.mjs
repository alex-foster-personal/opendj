/**
 * Phase-lock workload on REAL library grids, before and after the lock read
 * uneven grids smoothed (BEATSYNC-JITTER-01..05).
 *
 * The same simulation the unit suite uses (`tests/unit/phase-lock-workload.mjs`,
 * the ADR deck-beatgrid-source Amendment 1 model), driven twice per grid: once
 * with the lock as it was (`legacyPhaseLockDecision`) and once with the shipped
 * `phaseLockDecision`. Read-only: GET `/api/v1/tracks/{id}/anlz` one track at a
 * time, nothing is written to the engine. Tracks are named by stable id only.
 *
 * Requirements:
 *   ✔︎ ✅ The "before" column reproduces the ADR's published figure.
 *     [if] `--expect-before-median` is given and the flagged before-median is
 *     more than 1 re-join a minute away from it [then ⛔️]
 *     [if] any control (even) grid re-joins under either lock, or its trimming,
 *     error or base differs between the two locks [then ⛔️]
 *     [if] no flagged grid re-joins under the legacy lock [then ⛔️] (the run
 *     measured nothing)
 *   ✔︎ ✅ Never a partial table.
 *     [if] any id fails to fetch or has under 2 beats [then ⛔️] (exit 1, no table)
 *     [if] `--ids` has no `flagged` or no `control` list [then ⛔️]
 *     [if] the engine answers anything but 200 [then ⛔️]
 *
 * Usage (from apps/webui/frontend):
 *   node --experimental-strip-types tests/live/phase-lock-jitter-workload.mjs \
 *     --base http://127.0.0.1:8728 --ids <ids.json> --out <results.json> \
 *     [--expect-before-median 82.6]
 * `ids.json` is `{ "flagged": [stableId...], "control": [stableId...] }`.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';

import { loadTypeScriptModule } from '../unit/load-typescript.mjs';
import {
	WORKLOAD_PITCH_RANGE_PCT,
	legacyPhaseLockDecision,
	lockWorkload,
	median
} from '../unit/phase-lock-workload.mjs';

const { values: args } = parseArgs({
	options: {
		base: { type: 'string' },
		ids: { type: 'string' },
		out: { type: 'string' },
		'expect-before-median': { type: 'string' }
	}
});
for (const required of ['base', 'ids', 'out']) {
	if (args[required] === undefined) throw new Error(`--${required} is required`);
}

const ids = JSON.parse(readFileSync(args.ids, 'utf8'));
for (const group of ['flagged', 'control']) {
	if (!Array.isArray(ids[group]) || ids[group].length === 0) {
		throw new Error(`--ids must carry a non-empty "${group}" list`);
	}
}

const pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');

async function fetchBeats(stableId) {
	const reply = await fetch(`${args.base}/api/v1/tracks/${stableId}/anlz?points=100`, { method: 'GET' });
	if (reply.status !== 200) throw new Error(`${stableId}: anlz answered ${reply.status}`);
	const beats = (await reply.json()).beatgrid?.beats;
	if (!Array.isArray(beats) || beats.length < 2) throw new Error(`${stableId}: under 2 beats`);
	return beats;
}

/** One lock over one grid, plus the share of ticks its base sat on the pitch range. */
function measure(decide, followerBeats) {
	const range = WORKLOAD_PITCH_RANGE_PCT / 100;
	let ticks = 0;
	let atRange = 0;
	const result = lockWorkload({
		followerBeats,
		decide: (input) => {
			const decision = decide(input);
			ticks += 1;
			if (Math.abs(decision.base - 1) >= range * (1 - 1e-9)) atRange += 1;
			return decision;
		}
	});
	return {
		reseeksPerMin: result.reseeksPerMin,
		trimTimeFrac: result.trimTimeFrac,
		capTimeFrac: result.capTimeFrac,
		baseMovesPerMin: result.baseMovesPerMin,
		medianStoredErrMs: result.medianStoredErrMs,
		medianLockErrMs: result.medianLockErrMs,
		atPitchRangeFrac: atRange / ticks,
		minutes: result.minutes
	};
}

const rows = [];
for (const group of ['flagged', 'control']) {
	for (const stableId of ids[group]) {
		const beats = await fetchBeats(stableId); // one at a time, by design
		rows.push({
			group,
			stableId,
			beats: beats.length,
			before: measure(legacyPhaseLockDecision, beats),
			after: measure(pl.phaseLockDecision, beats)
		});
	}
}

const summary = (subset, side) => ({
	tracks: subset.length,
	reseeksPerMinMedian: median(subset.map((r) => r[side].reseeksPerMin)),
	reseeksPerMinMin: Math.min(...subset.map((r) => r[side].reseeksPerMin)),
	reseeksPerMinMax: Math.max(...subset.map((r) => r[side].reseeksPerMin)),
	tracksWithAnyReseek: subset.filter((r) => r[side].reseeksPerMin > 0).length,
	trimTimeMean: subset.reduce((sum, r) => sum + r[side].trimTimeFrac, 0) / subset.length,
	trimTimeMedian: median(subset.map((r) => r[side].trimTimeFrac)),
	medianStoredErrMs: median(subset.map((r) => r[side].medianStoredErrMs)),
	medianLockErrMs: median(subset.map((r) => r[side].medianLockErrMs)),
	baseMovesPerMinMedian: median(subset.map((r) => r[side].baseMovesPerMin))
});

const flagged = rows.filter((r) => r.group === 'flagged');
const control = rows.filter((r) => r.group === 'control');
// A flagged grid whose local tempo leaves the pitch range around the track's
// mean cannot be locked to a mean-tempo master by ANY trim: reported apart.
const inRange = flagged.filter((r) => r.after.atPitchRangeFrac === 0);
const outOfRange = flagged.filter((r) => r.after.atPitchRangeFrac > 0);
const groups = { flagged, 'flagged, tempo inside the pitch range': inRange, 'flagged, a section outside it': outOfRange, control };

// ---- the run must have measured something before any table is printed
const failures = [];
const flaggedBefore = summary(flagged, 'before');
if (flaggedBefore.tracksWithAnyReseek === 0) failures.push('no flagged grid re-joins under the legacy lock');
for (const side of ['before', 'after']) {
	const s = summary(control, side);
	if (s.reseeksPerMinMax !== 0) failures.push(`control re-joins ${side}: max ${s.reseeksPerMinMax}`);
}
// The control's invariant: an even grid is read as stored, so the lock does
// to it exactly what it did before, tick for tick.
for (const r of control) {
	for (const key of ['trimTimeFrac', 'capTimeFrac', 'medianStoredErrMs', 'medianLockErrMs', 'baseMovesPerMin']) {
		if (r.before[key] !== r.after[key]) {
			failures.push(`control ${r.stableId.slice(0, 10)} ${key} changed: ${r.before[key]} -> ${r.after[key]}`);
		}
	}
}
if (args['expect-before-median'] !== undefined) {
	const expected = Number(args['expect-before-median']);
	if (!(Math.abs(flaggedBefore.reseeksPerMinMedian - expected) <= 1)) {
		failures.push(`flagged before-median ${flaggedBefore.reseeksPerMinMedian} is not the expected ${expected}`);
	}
}
if (failures.length > 0) {
	console.error(`[ERROR] the run is not a measurement:\n  ${failures.join('\n  ')}`);
	process.exit(1);
}

const pct = (frac) => `${(frac * 100).toFixed(1)}%`;
const lines = [
	'| group | tracks | lock | re-joins/min median (min to max) | tracks with any re-join | time trimming mean (median) | median abs phase error ms, stored beats | same, as the lock reads it | base moves/min median |',
	'|---|---|---|---|---|---|---|---|---|'
];
const summaries = {};
for (const [name, subset] of Object.entries(groups)) {
	if (subset.length === 0) continue;
	summaries[name] = {};
	for (const side of ['before', 'after']) {
		const s = summary(subset, side);
		summaries[name][side] = s;
		lines.push(
			`| ${name} | ${s.tracks} | ${side} | ${s.reseeksPerMinMedian.toFixed(1)} (${s.reseeksPerMinMin.toFixed(1)} to ${s.reseeksPerMinMax.toFixed(1)}) | ${s.tracksWithAnyReseek} | ${pct(s.trimTimeMean)} (${pct(s.trimTimeMedian)}) | ${s.medianStoredErrMs.toFixed(2)} | ${s.medianLockErrMs.toFixed(2)} | ${s.baseMovesPerMinMedian.toFixed(0)} |`
		);
	}
}
lines.push('', '| stable id | beats | re-joins/min before | after | trimming before | after | median lock error ms after | share of time base sits on the pitch range |', '|---|---|---|---|---|---|---|---|');
for (const r of flagged) {
	lines.push(
		`| ${r.stableId.slice(0, 10)} | ${r.beats} | ${r.before.reseeksPerMin.toFixed(1)} | ${r.after.reseeksPerMin.toFixed(1)} | ${pct(r.before.trimTimeFrac)} | ${pct(r.after.trimTimeFrac)} | ${r.after.medianLockErrMs.toFixed(2)} | ${pct(r.after.atPitchRangeFrac)} |`
	);
}

writeFileSync(args.out, `${JSON.stringify({ pitchRangePct: WORKLOAD_PITCH_RANGE_PCT, summaries, rows }, null, '\t')}\n`);
console.log(lines.join('\n'));
