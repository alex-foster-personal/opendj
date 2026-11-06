/**
 * Source pins for the process-family snapshot module (PERF-UI-6785).
 *
 * Every test that reads a response body lives in
 * process-family-snapshot-fetch.test.mjs, where the body comes from the real
 * GET /api/v1/performance/telemetry/processes route (AGENTS.md: controls come
 * from the production endpoint or a verified capture, never hand-built). This
 * file keeps the monotonic-clock rule, plus the tenths-until-sum ACs (issue
 * #9317) that the live fixture walk cannot mint without a units change.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PERF-UI-6785 (Codex review on #6785): every age and interval in the snapshot
// module is monotonic. SOURCE PIN, because the behavior it guards (a wall
// clock moved BACK after a fetch) cannot be produced in a unit test without
// faking Date: this host has no faketime-style clock to inject into a real
// subprocess, and moving the real system clock would disturb every other
// process on the machine. The behavior tests in
// process-family-snapshot-fetch.test.mjs drive the same functions with
// explicit monotonic times.
test('the snapshot module measures ages on the monotonic clock, never Date', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/process-family-snapshot.ts', import.meta.url)),
		'utf8'
	);
	const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
	assert.doesNotMatch(code, /\bDate\b/, 'no wall-clock read or delta in code');
	assert.match(code, /performance\.now\(\)/, 'the monotonic clock is the one it reads');
	for (const fn of [
		'readFreshProcessFamilySnapshot',
		'processFamilyRefreshDue',
		'readUnavailableProbeStale',
		'processFamilySnapshotAgeMs'
	]) {
		assert.match(
			code,
			new RegExp(`export function ${fn}\\([^)]*monoNowMs: number = _monoNowMs\\(\\)`),
			fn
		);
	}
	const meters = readFileSync(
		fileURLToPath(new URL('../../src/lib/components/rb/PerfMeters.svelte', import.meta.url)),
		'utf8'
	);
	assert.doesNotMatch(
		meters,
		/(readFreshProcessFamilySnapshot|processFamilyRefreshDue|readUnavailableProbeStale|processFamilySnapshotAgeMs)\(Date\.now\(\)/
	);
});

let snapMod;
let meterMod;

before(async () => {
	snapMod = await loadTypeScriptModule('src/lib/rb/process-family-snapshot.ts');
	meterMod = await loadTypeScriptModule('src/lib/rb/memory-meter-model.ts');
});

/** Available body matching the route member contract; omit unused `role` (`role: null` is malformed). */
function tenthsFamilyBody(mb, count) {
	return {
		available: true,
		members: Array.from({ length: count }, (_, i) => ({
			name: i === 0 ? 'opendj-webcontent' : `opendj-sidecar-${i}`,
			source: 'live',
			rss_mb: mb,
			physical_footprint_mb: mb,
			...(i === 0 ? { role: 'webkit-webcontent' } : {})
		}))
	};
}

function readoutFromBody(body) {
	const answer = snapMod.parseProcessesResponse(body);
	assert.equal(answer.kind, 'available');
	return {
		snap: answer.snapshot,
		readout: meterMod.memoryReadout({
			jsHeapMB: null,
			pcmMB: 0,
			anlzMB: 0,
			anlzCount: 0,
			prefetchMB: 0,
			prefetchCount: 0,
			processes: answer.snapshot.members
		})
	};
}

// The route already reports tenths; the live fixture walk cannot mint 0.4 MB
// members without a units change, so these ACs compose parseProcessesResponse
// with memoryReadout instead of walking the fixture server.
test('ten members of 0.4 MB each total 4 MB in the readout', () => {
	const { snap, readout } = readoutFromBody(tenthsFamilyBody(0.4, 10));
	assert.equal(snap.members.length, 10);
	for (const member of snap.members) {
		assert.equal(member.mb, 0.4, 'the member keeps tenths, not a rounded 0');
		assert.equal(member.liveMb, 0.4);
	}
	assert.equal(readout.totalMB, 4);
	assert.equal(readout.text, '4M');
	assert.equal(readout.processesMB, 4);
});

test('a single member of 12.6 MB still displays as 13 MB', () => {
	const { snap, readout } = readoutFromBody(tenthsFamilyBody(12.6, 1));
	assert.equal(snap.members[0].mb, 12.6);
	assert.equal(readout.totalMB, 13);
	assert.equal(readout.text, '13M');
	assert.equal(readout.processesMB, 13);
	assert.match(readout.hover, /12\.6 MB/, 'hover still names the member in tenths');
});
