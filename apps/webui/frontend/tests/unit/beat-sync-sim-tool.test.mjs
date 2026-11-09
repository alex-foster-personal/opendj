/**
 * tools/beat-sync-sim/sim.mjs drives the shipped phase lock on a grid pair and
 * prints one JSON line of lock statistics (the instrument behind PR #8872).
 *
 * [if] two identical constant 120 BPM grids are simulated for 8 s, with the tool
 *   invoked from the repository root on RELATIVE paths as the README documents
 *   [then] it prints JSON with 0 re-seeks and a p95 phase error under 5 ms
 *   [⛔️ if the tool errors (a relative path resolved after chdir), re-seeks a
 *   follower that is already in phase, or prints no JSON]
 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const REPO_ROOT = resolve(FRONTEND, '../../..');
const SIM = 'tools/beat-sync-sim/sim.mjs';
const FRONTEND_RELATIVE = 'apps/webui/frontend';

/** A constant grid: `count` beats at `bpm` from `firstSec`, bar numbers cycling 1 to 4. */
function grid(bpm, count, firstSec = 0.1) {
	const period = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({
		n: (i % 4) + 1,
		bpm,
		t: Number((firstSec + i * period).toFixed(5))
	}));
}

test('sim.mjs, run from the repo root on relative paths, reports 0 re-seeks for two identical constant grids', () => {
	const dir = mkdtempSync(join(tmpdir(), 'beat-sync-sim-'));
	const beatsPath = join(dir, 'beats.json');
	writeFileSync(beatsPath, JSON.stringify({ out: grid(120, 400), in: grid(120, 400) }));
	const beatsRelative = relative(REPO_ROOT, beatsPath);
	const stdout = execFileSync(
		process.execPath,
		[SIM, FRONTEND_RELATIVE, beatsRelative, '10', '10', '8', '16', '1'],
		{ cwd: REPO_ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }
	);
	const lines = stdout.trim().split('\n');
	const stats = JSON.parse(lines.at(-1));
	assert.equal(stats.reseeks, 0, `re-seeks: ${JSON.stringify(stats)}`);
	assert.ok(stats.p95Ms < 5, `p95 ${stats.p95Ms} ms: ${JSON.stringify(stats)}`);
	assert.ok(stats.measuredSec > 0, `nothing measured: ${JSON.stringify(stats)}`);
});
