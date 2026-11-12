/**
 * PERF-UI-6785 (Codex review on #6785): what fetches of
 * GET /api/v1/performance/telemetry/processes do to the cached snapshot,
 * against the REAL route served by fixtures/process_family_server.py: the
 * production create_app, a real psutil walk (rooted at the server itself, or
 * at a real exited child for a genuinely empty walk) and the locked real
 * probe capture ((private path), checksum-verified
 * against its MANIFEST and read by the route's own reader). Nothing here is
 * hand-built; tests that need a fresh probe record or a drifted body drive
 * the fixture server's /test/probe/fresh and /test/drift-next controls.
 *
 * Regression lines:
 * - if an UNAVAILABLE answer keeps the last snapshot, then stale totals show
 *   as current and the stale-probe warning never appears
 * - if a FAILED request (the engine is gone) drops the snapshot, then one
 *   lost request blanks the figure; it ages out on its own clock instead
 * - if the refresh throttles on the last usable snapshot, then an unavailable
 *   or failing engine is refetched on every tick
 * - if a failed attempt blocks refreshes for good, then the meter never recovers
 * - if an OLDER answer that arrives after a newer one is applied, then it
 *   clears a newer snapshot or restores withdrawn totals
 * - if no answer is applied while another is pending, then a newer answer
 *   waits on an older, slow one
 * - if every answer but the latest attempt's is dropped, then an engine slower
 *   than the poll interval starves the cache: each answer lands while the
 *   next request is already in flight
 * - if the expiry applies only to the compact figure, then the pinned
 *   breakdown lists processes that aged out as current
 * - if the probe-derived parts expire only with the snapshot (30 s from the
 *   fetch), then a sample fetched at 44 s old is shown as current for up to
 *   30 s past its own 45 s limit
 * - if they expire early (the overshoot), then a fresh sample's footprints
 *   vanish before its lifetime ends
 * - if the probe age counts on from the REQUEST START (Sol on #6785), then a
 *   slow walk is counted twice (it is already inside the age the engine
 *   returned) and a sample is retired before its lifetime ends; if the walk's
 *   expiry counts from the RESPONSE instead (the overshoot), then a slow
 *   walk is shown as current for longer than the expiry allows
 * - if a MALFORMED 200 body (`{}`, `available: "true"`, a string `stale`)
 *   clears the snapshot (Sol on #6785), then contract drift blanks the figure
 *   as if the engine had said unavailable; it is a failed request instead,
 *   and only an explicit, valid `available: false` clears (the control)
 * - if the fixture server's scratch cleanup is not real, then a run leaks
 *   files into the next; the directory must be gone after the server exits
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { fixtureServerLaunch } from '../../scripts/fixture-server.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';
import { stopFixtureServer } from './fixtures/stop-fixture-server.mjs';

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const SERVER_SCRIPT = fileURLToPath(
	new URL('./fixtures/process_family_server.py', import.meta.url)
);
/** Timestamp of the LAST sample in the locked capture: the one the route's reader returns. */
const CAPTURED_LAST_SAMPLE = '2026-09-01T15:33:06.876Z';

let serverProcess;
let apiBase;
let mod;
/** The fixture server's scratch directory, which it must remove on exit. */
let scratchDir;

async function control(method, path) {
	const res = await fetch(`${apiBase}${path}`, { method });
	assert.equal(res.status, 200, `${method} ${path} answered ${res.status}`);
	return res.json();
}

/** What the real route answers right now, read without the module under test. */
async function routeBody() {
	const res = await fetch(`${apiBase}/api/v1/performance/telemetry/processes`);
	assert.equal(res.status, 200);
	return res.json();
}

/**
 * Runs `run` on the REAL route body in the fresh-probe state (several members,
 * live and probe-derived), then resets the probe to the captured sample.
 */
async function withFreshProbe(run) {
	await control('POST', '/test/family/self');
	await control('POST', '/test/probe/fresh?age_s=0');
	try {
		return await run(await routeBody());
	} finally {
		await control('POST', '/test/probe/captured');
	}
}

/**
 * A copy of a body the route really sent, with ONE named change. Every
 * negative case in this file is one of these, never a body typed by hand.
 */
function derived(body, change) {
	const copy = structuredClone(body);
	change(copy);
	return copy;
}

async function waitForHeld(count) {
	const deadline = Date.now() + 15_000;
	while ((await control('GET', '/test/held')).held !== count) {
		if (Date.now() > deadline) throw new Error(`no response held after 15 s (wanted ${count})`);
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
}

/** Lets a held response go even when an assertion failed, so a red run ends instead of hanging. */
async function releaseHeld(pending) {
	await control('POST', '/test/release');
	await pending;
	assert.equal((await control('GET', '/test/held')).held, 0);
}

before(async () => {
	const launch = fixtureServerLaunch({ repoRoot: REPOSITORY_ROOT, pythonTarget: SERVER_SCRIPT });
	serverProcess = spawn(launch.command, launch.args, {
		cwd: launch.cwd,
		env: launch.env,
		stdio: ['ignore', 'pipe', 'inherit']
	});
	const port = await new Promise((resolve, reject) => {
		const lines = createInterface({ input: serverProcess.stdout });
		serverProcess.once('exit', (code) =>
			reject(new Error(`fixture server exited early (${code})`))
		);
		lines.on('line', (line) => {
			const scratch = /^SCRATCH (.+)$/.exec(line);
			if (scratch) scratchDir = scratch[1];
			const match = /^READY (\d+)$/.exec(line);
			if (match) resolve(Number(match[1]));
		});
	});
	apiBase = `http://127.0.0.1:${port}`;
	assert.ok(
		scratchDir && existsSync(scratchDir),
		`the server named a scratch dir that exists: ${scratchDir}`
	);
	mod = await loadTypeScriptModule('src/lib/rb/process-family-snapshot.ts', {
		viteApiBase: apiBase
	});
});

after(async () => {
	if (serverProcess) await stopFixtureServer(serverProcess, apiBase);
	// The server exited 0 (stopFixtureServer checks), and its cleanup was real.
	assert.equal(existsSync(scratchDir), false, `scratch dir left behind: ${scratchDir}`);
});

test('the fixture serves the real route in both states', async () => {
	await control('POST', '/test/family/self');
	const available = await routeBody();
	assert.equal(available.available, true);
	assert.ok(available.members.length > 0 && available.members.every((m) => m.source === 'live'));
	await control('POST', '/test/family/exited');
	const unavailable = await routeBody();
	assert.equal(unavailable.available, false);
	assert.equal(unavailable.stale, true);
	// The age is the captured sample's own: the route read the real capture.
	const capturedAge = (Date.now() - Date.parse(CAPTURED_LAST_SAMPLE)) / 1000;
	assert.ok(
		Math.abs(unavailable.age_seconds - capturedAge) < 60,
		`age ${unavailable.age_seconds} vs capture ${capturedAge}`
	);
	assert.equal('members' in unavailable, false);
});

// The parser on REAL route bodies (read without the module under test, then
// handed to processFamilyFrom / probeStaleFrom): each protocol state below is
// produced by the route from the verified capture, not written here.
test("the parser keeps each member's probe role and fields, from a real body", async () => {
	await control('POST', '/test/family/self');
	await control('POST', '/test/probe/fresh?age_s=0');
	try {
		const body = await routeBody();
		const snap = mod.processFamilyFrom(body);
		const engine = snap.members.filter((m) => !m.fromProbe && m.role === 'python-engine');
		assert.equal(engine.length, 1, "the live engine took the captured engine row's role");
		assert.deepEqual(engine[0].probeFields, ['role', 'physical_footprint_mb']);
		const routeEngine = body.members.find((m) => m.role === 'python-engine');
		assert.equal(engine[0].mb, routeEngine.physical_footprint_mb, 'its MB is the probe footprint');
		assert.equal(engine[0].liveMb, routeEngine.rss_mb, 'its live MB is the walk RSS');
		const probeRoles = snap.members
			.filter((m) => m.fromProbe)
			.map((m) => m.role)
			.sort();
		assert.deepEqual(probeRoles, [
			'desktop-shell',
			'webkit-gpu',
			'webkit-networking',
			'webkit-webcontent',
			'webkit-webcontent'
		]);
		assert.ok(snap.members.some((m) => m.fromProbe && m.label === 'opendj-desktop'));
		assert.ok(snap.members.filter((m) => !m.fromProbe).every((m) => m.liveMb !== null));
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('the parser carries the stale flag and age, from real bodies', async () => {
	await control('POST', '/test/family/self');
	const stale = await routeBody();
	assert.equal(stale.stale, true);
	const staleSnap = mod.processFamilyFrom(stale);
	assert.equal(staleSnap.probeStale, true);
	assert.equal(staleSnap.probeAgeSeconds, stale.age_seconds);
	assert.ok(
		staleSnap.members.every((m) => !m.fromProbe && m.role === null),
		'nothing of a stale sample'
	);
	try {
		// control: a fresh sample, and no sample at all, both read not stale
		await control('POST', '/test/probe/fresh?age_s=0');
		assert.equal(mod.processFamilyFrom(await routeBody()).probeStale, false);
		await control('POST', '/test/probe/none');
		const none = mod.processFamilyFrom(await routeBody());
		assert.equal(none.probeStale, false);
		assert.equal(none.probeAgeSeconds, null);
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('an unavailable body still carries the stale-probe diagnostic, from real bodies', async () => {
	await control('POST', '/test/family/exited');
	const body = await routeBody();
	assert.equal(body.available, false);
	assert.equal(mod.processFamilyFrom(body), null, 'no current member to show');
	assert.deepEqual(mod.probeStaleFrom(body), { ageSeconds: body.age_seconds });
	try {
		// control: unavailable with no sample, and available with a fresh one
		await control('POST', '/test/probe/none');
		assert.equal(mod.probeStaleFrom(await routeBody()), null);
		await control('POST', '/test/family/self');
		await control('POST', '/test/probe/fresh?age_s=0');
		assert.equal(mod.probeStaleFrom(await routeBody()), null);
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('an unavailable answer drops the snapshot and keeps the stale warning; an available one restores both', async () => {
	await control('POST', '/test/family/self');
	assert.notEqual(await mod.fetchProcessFamilySnapshot(), null);
	assert.ok(mod.readProcessFamilySnapshot().members.length > 0);

	await control('POST', '/test/family/exited');
	assert.equal(await mod.fetchProcessFamilySnapshot(), null);
	assert.equal(mod.readProcessFamilySnapshot(), null, 'withdrawn totals are not shown as current');
	assert.equal(mod.processFamilySnapshotAgeMs(), null);
	const warning = mod.readUnavailableProbeStale(30_000);
	assert.ok(warning !== null && warning.ageSeconds > 45, 'the stale sample is named');

	await control('POST', '/test/family/self');
	await mod.fetchProcessFamilySnapshot();
	assert.notEqual(mod.readProcessFamilySnapshot(), null);
	assert.equal(mod.readUnavailableProbeStale(30_000), null);
});

test('an older answer arriving after a newer one is dropped, in both directions', async () => {
	// Older AVAILABLE held, newer UNAVAILABLE arrives first: the newer one applies
	// at once (not held up by the pending one), and the late available answer
	// must not restore the withdrawn totals.
	await control('POST', '/test/family/self');
	await control('POST', '/test/hold-next');
	const older = mod.fetchProcessFamilySnapshot();
	try {
		await waitForHeld(1);
		await control('POST', '/test/family/exited');
		assert.equal(await mod.fetchProcessFamilySnapshot(), null);
		assert.equal(
			mod.readProcessFamilySnapshot(),
			null,
			'the newer answer applied while the older one was pending'
		);
	} finally {
		await releaseHeld(older);
	}
	assert.equal(mod.readProcessFamilySnapshot(), null, 'the late available answer was dropped');
	assert.notEqual(mod.readUnavailableProbeStale(30_000), null);

	// Older UNAVAILABLE held, newer AVAILABLE arrives first: the late
	// unavailable answer must not clear the newer snapshot.
	await control('POST', '/test/hold-next');
	const olderUnavailable = mod.fetchProcessFamilySnapshot();
	try {
		await waitForHeld(1);
		await control('POST', '/test/family/self');
		assert.notEqual(await mod.fetchProcessFamilySnapshot(), null);
		assert.notEqual(
			mod.readProcessFamilySnapshot(),
			null,
			'the newer answer applied while the older one was pending'
		);
	} finally {
		await releaseHeld(olderUnavailable);
	}
	assert.notEqual(mod.readProcessFamilySnapshot(), null, 'the late unavailable answer was dropped');
	assert.equal(mod.readUnavailableProbeStale(30_000), null);
});

test('under polling slower than the interval, every answer lands while the next is in flight and is still applied', async () => {
	// Start empty, then alternate the real family state per request, so each
	// applied answer is visible: an available one fills the cache, an
	// unavailable one clears it.
	await control('POST', '/test/family/exited');
	assert.equal(await mod.fetchProcessFamilySnapshot(), null);
	const states = ['self', 'exited', 'self', 'exited'];
	const inflight = [];
	try {
		for (let i = 0; i < states.length; i++) {
			await control('POST', `/test/family/${states[i]}`);
			await control('POST', '/test/hold-next');
			inflight.push(mod.fetchProcessFamilySnapshot());
			await waitForHeld(i === 0 ? 1 : 2);
			if (i === 0) continue;
			// Answer i-1 lands now, with answer i still in flight.
			await control('POST', '/test/release');
			await inflight[i - 1];
			assert.equal(
				mod.readProcessFamilySnapshot() !== null,
				states[i - 1] === 'self',
				`answer ${i} (${states[i - 1]}) applied while answer ${i + 1} was in flight`
			);
		}
	} finally {
		while ((await control('GET', '/test/held')).held > 0) await control('POST', '/test/release');
		await Promise.all(inflight);
	}
	assert.equal(mod.readProcessFamilySnapshot(), null, 'the last answer (exited) applied too');
});

test('a snapshot older than the expiry is not current, though the cache keeps it', async () => {
	const maxAge = 30_000;
	await control('POST', '/test/family/self');
	assert.notEqual(await mod.fetchProcessFamilySnapshot(), null);
	const now = performance.now();
	const fetchedAt = now - mod.processFamilySnapshotAgeMs(now);
	assert.notEqual(
		mod.readFreshProcessFamilySnapshot(maxAge, fetchedAt + maxAge - 1),
		null,
		'current up to the expiry'
	);
	assert.equal(
		mod.readFreshProcessFamilySnapshot(maxAge, fetchedAt + maxAge + 1),
		null,
		'not current after it'
	);
	assert.notEqual(
		mod.readProcessFamilySnapshot(),
		null,
		'the cache itself is kept (a failure is not news)'
	);

	await control('POST', '/test/family/exited');
	await mod.fetchProcessFamilySnapshot();
	assert.equal(mod.readFreshProcessFamilySnapshot(maxAge), null, 'unavailable is never current');
});

test('probe-derived members leave once the sample itself is 45 s old, counting its age when fetched', async () => {
	await control('POST', '/test/family/self');
	await control('POST', '/test/probe/fresh?age_s=40');
	try {
		const snapshot = await mod.fetchProcessFamilySnapshot();
		const after = performance.now();
		assert.ok(snapshot !== null && snapshot.probeStale === false, 'the 40 s old sample is fresh');
		assert.ok(
			snapshot.probeAgeSeconds >= 40 && snapshot.probeAgeSeconds < 42,
			`${snapshot.probeAgeSeconds}`
		);
		const probeOnly = snapshot.members.filter((m) => m.fromProbe);
		assert.equal(
			probeOnly.length,
			5,
			'the capture rows other than the engine are probe-only members'
		);
		const enriched = snapshot.members.filter((m) => !m.fromProbe && m.role === 'python-engine');
		assert.equal(enriched.length, 1, 'the live engine took its role from the captured engine row');
		assert.deepEqual(enriched[0].probeFields, ['role', 'physical_footprint_mb']);
		const liveCount = snapshot.members.length - probeOnly.length;

		// About 4 s of the sample's lifetime remain: 3 s on it is still shown.
		const within = mod.readFreshProcessFamilySnapshot(30_000, after + 3_000);
		assert.equal(within.members.filter((m) => m.fromProbe).length, 5);
		assert.equal(within.probeStale, false);

		// 6 s on it is past 45 s: its rows and fields go, the live walk stays.
		const past = mod.readFreshProcessFamilySnapshot(30_000, after + 6_000);
		assert.notEqual(past, null, 'the live members are still current');
		assert.equal(past.members.filter((m) => m.fromProbe).length, 0);
		assert.equal(past.members.length, liveCount);
		assert.ok(
			past.members.every((m) => m.role !== 'python-engine'),
			'the probe role is given back'
		);
		// The engine took the probe's footprint field too; its MB stays the
		// walk's own reading, which it has (the control: live RSS is kept).
		const engineBefore = snapshot.members.find((m) => m.role === 'python-engine');
		const engineAfter = past.members.find(
			(m) =>
				m.label === engineBefore.label &&
				m.probeFields.length === 0 &&
				m.liveMb === engineBefore.liveMb
		);
		assert.ok(engineAfter && engineAfter.mb === engineBefore.liveMb && engineAfter.mb !== null);
		assert.ok(
			past.members.every((m) => m.mb === m.liveMb),
			'every remaining MB is a live reading'
		);
		assert.equal(past.probeStale, true);
		assert.ok(past.probeAgeSeconds > 45);
		assert.equal(
			mod.readProcessFamilySnapshot().members.filter((m) => m.fromProbe).length,
			5,
			'the cache is untouched'
		);
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('a fresh probe sample is shown for its whole remaining lifetime', async () => {
	await control('POST', '/test/family/self');
	await control('POST', '/test/probe/fresh?age_s=10');
	try {
		const snapshot = await mod.fetchProcessFamilySnapshot();
		const after = performance.now();
		assert.equal(snapshot.members.filter((m) => m.fromProbe).length, 5);
		const maxAge = 30_000;
		const fetchedAt = after - mod.processFamilySnapshotAgeMs(after);
		// One second shy of the walk expiry, from request start (not response):
		// the sample is about 39 s old with about 6 s left; still shown in full.
		const late = mod.readFreshProcessFamilySnapshot(maxAge, fetchedAt + maxAge - 1_000);
		assert.notEqual(late, null, 'one second shy of the walk expiry is still inside its window');
		assert.equal(late.members.filter((m) => m.fromProbe).length, 5);
		assert.ok(late.members.some((m) => m.role === 'python-engine'));
		assert.equal(late.probeStale, false);
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('a slow walk is not counted twice: the probe ages on from the response, the walk expires from the request', async () => {
	// The route sleeps 4 s BEFORE it runs (a slow walk), so the age it
	// returns for this 38 s old sample already holds those 4 s: about 42 s,
	// plus whatever the real psutil walk costs on this host.
	await control('POST', '/test/family/self');
	await control('POST', '/test/probe/fresh?age_s=38');
	try {
		await control('POST', '/test/delay-next?s=4');
		const before = performance.now();
		const snapshot = await mod.fetchProcessFamilySnapshot();
		const after = performance.now();
		assert.ok(after - before >= 4_000, `the request took ${after - before} ms`);
		assert.equal(snapshot.probeStale, false);
		const elapsedS = (after - before) / 1000;
		assert.ok(
			snapshot.probeAgeSeconds >= 42,
			`the delay is missing from the age: ${snapshot.probeAgeSeconds}`
		);
		// Age is 38 plus the delay plus the real psutil walk, which already sits
		// inside elapsedS. A second copy of the delay would cross the freshness wall.
		assert.ok(
			snapshot.probeAgeSeconds < 38 + elapsedS + 2 &&
				snapshot.probeAgeSeconds < mod.PROBE_SAMPLE_FRESH_SECONDS,
			`sample age ${snapshot.probeAgeSeconds}s, elapsed=${elapsedS}s, ` +
				`freshness=${mod.PROBE_SAMPLE_FRESH_SECONDS}s`
		);

		const remainingMs = (mod.PROBE_SAMPLE_FRESH_SECONDS - snapshot.probeAgeSeconds) * 1000;
		assert.ok(
			remainingMs > 0,
			`no lifetime left at fetch: ${snapshot.probeAgeSeconds}/${mod.PROBE_SAMPLE_FRESH_SECONDS}s`
		);

		// Just inside the remaining lifetime, counted from the response: still shown.
		// Counted from the request start this same instant includes the slow walk
		// again and would already be gone.
		const within = mod.readFreshProcessFamilySnapshot(
			30_000,
			after + Math.max(0, remainingMs - 200)
		);
		assert.notEqual(within, null, 'probe still inside its 45 s lifetime from the response');
		assert.equal(within.members.filter((m) => m.fromProbe).length, 5);
		assert.equal(within.probeStale, false);
		const past = mod.readFreshProcessFamilySnapshot(30_000, after + remainingMs + 200);
		assert.notEqual(past, null, 'the live walk is still inside its 30 s window');
		assert.equal(past.members.filter((m) => m.fromProbe).length, 0);
		assert.equal(past.probeStale, true);

		// The walk's own expiry stays on the request start (conservative): just
		// past 30 s from it, nothing is current, though only about 26 s have
		// passed since the answer. The module stamps the request start itself,
		// a moment after `before`; read that stamp rather than assume it is
		// `before` (a 1 ms margin that a loaded CI host overruns; main uses 1 s
		// on `before`, this uses the module stamp for the same conservative TTL).
		const startedAt = after - mod.processFamilySnapshotAgeMs(after);
		assert.ok(startedAt >= before, `the stamp predates the request: ${startedAt} < ${before}`);
		assert.ok(
			after - startedAt >= 4_000,
			`the walk's expiry counts from the response, not the request: ${after - startedAt} ms`
		);
		assert.equal(mod.readFreshProcessFamilySnapshot(30_000, startedAt + 30_001), null);
		assert.notEqual(mod.readFreshProcessFamilySnapshot(30_000, startedAt + 29_000), null);
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('a malformed body is a failed request: the snapshot is kept; an explicit unavailable still clears it', async () => {
	await control('POST', '/test/family/self');
	assert.notEqual(await mod.fetchProcessFamilySnapshot(), null);
	const kept = mod.readProcessFamilySnapshot();
	const keptAge = mod.processFamilySnapshotAgeMs();

	// Each drift rewrites the route's REAL body, available and unavailable.
	const drifts = [
		['self', 'empty'],
		['self', 'string-available'],
		['self', 'string-stale'],
		['exited', 'empty'],
		['exited', 'string-available'],
		['exited', 'string-stale']
	];
	for (const [family, mode] of drifts) {
		await control('POST', `/test/family/${family}`);
		await control('POST', `/test/drift-next?mode=${mode}`);
		await mod.fetchProcessFamilySnapshot();
		assert.equal(mod.readProcessFamilySnapshot(), kept, `${family}/${mode}: the snapshot is kept`);
		assert.ok(
			mod.processFamilySnapshotAgeMs() >= keptAge,
			`${family}/${mode}: its clock was not restarted`
		);
		assert.notEqual(
			mod.readFreshProcessFamilySnapshot(30_000),
			null,
			`${family}/${mode}: still current`
		);
	}
	// The drift really reached the module: the same bodies, parsed.
	await control('POST', '/test/family/exited');
	await control('POST', '/test/drift-next?mode=string-available');
	const drifted = await routeBody();
	assert.equal(drifted.available, 'false');
	assert.equal(mod.parseProcessesResponse(drifted).kind, 'malformed');

	// control: the real, valid `available: false` body clears it.
	await mod.fetchProcessFamilySnapshot();
	assert.equal(mod.readProcessFamilySnapshot(), null, 'an explicit unavailable is news');
	assert.notEqual(mod.readUnavailableProbeStale(30_000), null);
});

// Codex on #6785 (BLOCKING): one bad member entry used to be skipped, so the
// body still counted as available and replaced the last good snapshot with a
// partial one. Now the whole body is malformed and the cache is kept.
test('a body with one malformed member keeps the snapshot; a fully valid one with several members replaces it', async () => {
	await control('POST', '/test/family/self');
	// A fresh probe record merges its rows, so the real body has several members.
	await control('POST', '/test/probe/fresh?age_s=0');
	try {
		assert.notEqual(await mod.fetchProcessFamilySnapshot(), null);
		const kept = mod.readProcessFamilySnapshot();
		assert.ok(
			kept.members.length >= 2,
			`the real body has several members: ${kept.members.length}`
		);

		for (const mode of ['null-member', 'string-rss']) {
			await control('POST', `/test/drift-next?mode=${mode}`);
			await mod.fetchProcessFamilySnapshot();
			assert.equal(mod.readProcessFamilySnapshot(), kept, `${mode}: the snapshot is kept`);
		}
		// The drift really reached the parser: the same body, parsed.
		await control('POST', '/test/drift-next?mode=null-member');
		const drifted = await routeBody();
		assert.equal(drifted.members.at(-1), null);
		assert.deepEqual(mod.parseProcessesResponse(drifted), {
			kind: 'malformed',
			reason: `member ${drifted.members.length - 1} is not an object`
		});

		// control (the overshoot): the real, fully valid body with several members replaces it.
		const valid = await routeBody();
		assert.ok(valid.members.length >= 2);
		const replaced = await mod.fetchProcessFamilySnapshot();
		assert.notEqual(replaced, null);
		assert.notEqual(mod.readProcessFamilySnapshot(), kept, 'a valid answer is applied');
		assert.equal(replaced.members.length, valid.members.length, 'every member is kept');
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

// Codex on #6785 (P1): the parser contract is checked on REAL route bodies.
// Each negative is a named change to a body the route sent (derived), so a
// route change can break these tests instead of hiding behind a hand-built body.
test('processFamilyFrom reads the real route body: every member kept, labels and MB from the route', async () => {
	await withFreshProbe((body) => {
		assert.equal(body.available, true);
		const snap = mod.processFamilyFrom(body);
		assert.ok(snap, 'the real available body is an answer');
		assert.equal(snap.members.length, body.members.length, 'no member dropped');
		assert.deepEqual(
			snap.members.map((m) => m.label),
			body.members.map((m) => m.name)
		);
		assert.equal(snap.members[0].fromProbe, false);
		assert.equal(
			snap.members[0].mb,
			body.members[0].physical_footprint_mb ?? body.members[0].rss_mb
		);
		assert.ok(snap.members.some((m) => m.fromProbe && m.label === 'opendj-desktop'));
	});
});

test('a live member with no MB reading stays listed, with null MB (derived: MB fields removed)', async () => {
	await withFreshProbe((body) => {
		const unreadable = derived(body, (b) => {
			delete b.members[0].rss_mb;
			delete b.members[0].physical_footprint_mb;
		});
		const snap = mod.processFamilyFrom(unreadable);
		assert.ok(snap);
		assert.equal(snap.members[0].label, unreadable.members[0].name);
		assert.equal(snap.members[0].mb, null);
	});
});

test('kernel pressure is absent when the body omits it, never 0 (derived: omitted, kernel 0 and 2)', async () => {
	await withFreshProbe((body) => {
		assert.equal(
			Object.hasOwn(body, 'kernel_memory_pressure_level'),
			false,
			'the real body has no kernel field'
		);
		const withoutKernel = derived(body, (b) => delete b.kernel_memory_pressure_level);
		assert.equal(Object.hasOwn(withoutKernel, 'kernel_memory_pressure_level'), false);
		const snap = mod.processFamilyFrom(withoutKernel);
		assert.equal(snap.kernelLevel, null);
		assert.equal(
			mod.processFamilyFrom(derived(body, (b) => (b.kernel_memory_pressure_level = 0))).kernelLevel,
			null
		);
		assert.equal(
			mod.processFamilyFrom(derived(body, (b) => (b.kernel_memory_pressure_level = 2))).kernelLevel,
			2
		);
	});
});

test('the legacy by_role_mb fallback maps probe slugs to opendj labels (derived: members deleted)', async () => {
	await withFreshProbe((body) => {
		const legacy = derived(body, (b) => delete b.members);
		const snap = mod.processFamilyFrom(legacy);
		assert.ok(snap, 'a legacy body with by_role_mb is still an answer');
		const labels = snap.members.map((m) => m.label).sort();
		assert.deepEqual(labels, [
			'opendj-desktop',
			'opendj-engine',
			'opendj-gpu',
			'opendj-networking',
			'opendj-webcontent'
		]);
	});
});

test('parseProcessesResponse: the real bodies are answers; each named drift of one is malformed', async () => {
	await withFreshProbe((body) => {
		assert.equal(mod.parseProcessesResponse(body).kind, 'available');
		const negatives = {
			'members emptied': (b) => (b.members = []),
			'a null member appended': (b) => b.members.push(null),
			'a string member appended': (b) => b.members.push('opendj-worker'),
			'a member with no name': (b) => delete b.members[0].name,
			'a string rss_mb': (b) => (b.members[0].rss_mb = '1'),
			'a negative rss_mb': (b) => (b.members[0].rss_mb = -1),
			'a negative physical_footprint_mb': (b) => (b.members[0].physical_footprint_mb = -0.1),
			'a negative compressed_mb': (b) => (b.compressed_mb = -4),
			'a negative churn_score': (b) => (b.churn_score = -2),
			'a negative compressor_rate': (b) => (b.compressor_rate = -1),
			'a negative legacy by_role_mb value': (b) => {
				delete b.members;
				b.by_role_mb = { 'python-engine': -1 };
			},
			'a non-finite physical_footprint_mb': (b) =>
				(b.members[0].physical_footprint_mb = Number.NaN),
			'a non-string role': (b) => (b.members[0].role = 7),
			'an unknown source': (b) => (b.members[1].source = 'guess'),
			'probe_fields with a non-string': (b) => (b.members[0].probe_fields = ['role', 3]),
			'probe_fields as a string': (b) => (b.members[0].probe_fields = 'role'),
			'a string churn_score': (b) => (b.churn_score = '9'),
			'a null compressed_mb': (b) => (b.compressed_mb = null),
			'available as a string': (b) => (b.available = 'true'),
			'a string age_seconds': (b) => (b.age_seconds = '3'),
			'a string stale': (b) => (b.stale = 'false'),
			'legacy map with no members and an empty map': (b) => {
				delete b.members;
				b.by_role_mb = {};
			},
			'legacy map with an empty role key': (b) => {
				delete b.members;
				b.by_role_mb = { '': 1 };
			},
			'legacy map with a non-finite MB': (b) => {
				delete b.members;
				b.by_role_mb = { 'python-engine': 'x' };
			},
			'no member list and no legacy map': (b) => {
				delete b.members;
				delete b.by_role_mb;
			},
			'every field removed': (b) => {
				for (const key of Object.keys(b)) delete b[key];
			}
		};
		for (const [name, change] of Object.entries(negatives)) {
			assert.equal(mod.parseProcessesResponse(derived(body, change)).kind, 'malformed', name);
		}
		// controls: shapes the route really sends, derived by keeping them
		assert.equal(
			mod.parseProcessesResponse(derived(body, (b) => (b.members = b.members.slice(0, 1)))).kind,
			'available',
			'one valid member is an answer'
		);
		assert.equal(
			mod.parseProcessesResponse(derived(body, (b) => (b.members[1].probe_fields = []))).kind,
			'available',
			'an empty probe_fields list is what the route sends when the probe row had no probe-only field'
		);
		// zero is a reading: a member at 0 MB and a zero measurement are answers
		const zeros = mod.parseProcessesResponse(
			derived(body, (b) => {
				b.members[0].rss_mb = 0;
				b.members[0].physical_footprint_mb = 0;
				b.churn_score = 0;
				b.compressed_mb = 0;
			})
		);
		assert.equal(zeros.kind, 'available', 'zero MB and zero measurements are valid');
		assert.equal(zeros.snapshot.members[0].mb, 0);
	});
	// The real unavailable body is an answer; a drifted one is not.
	await control('POST', '/test/family/exited');
	const gone = await routeBody();
	assert.equal(
		mod.parseProcessesResponse(gone).kind,
		'unavailable',
		'the real unavailable body is an answer'
	);
	assert.equal(
		mod.parseProcessesResponse(derived(gone, (b) => (b.stale = 'true'))).kind,
		'malformed'
	);
	// Shapes that are not a route body at all: labeled negatives, derived from a real body.
	await withFreshProbe((body) => {
		for (const [name, value] of [
			['a JSON array of the body', [body]],
			['the body as a string', JSON.stringify(body)],
			['a number', 42],
			['null', null]
		]) {
			assert.equal(mod.parseProcessesResponse(value).kind, 'malformed', name);
		}
	});
});

test('a negative MB keeps the snapshot; a zero MB is a reading and replaces it', async () => {
	await control('POST', '/test/family/self');
	await control('POST', '/test/probe/fresh?age_s=0');
	try {
		assert.notEqual(await mod.fetchProcessFamilySnapshot(), null);
		const kept = mod.readProcessFamilySnapshot();
		await control('POST', '/test/drift-next?mode=negative-mb');
		await mod.fetchProcessFamilySnapshot();
		assert.equal(mod.readProcessFamilySnapshot(), kept, 'negative-mb: the snapshot is kept');
		await control('POST', '/test/drift-next?mode=zero-mb');
		const replaced = await mod.fetchProcessFamilySnapshot();
		assert.equal(replaced.members[0].mb, 0, 'zero-mb: the zero is shown as a reading');
		assert.notEqual(mod.readProcessFamilySnapshot(), kept);
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('a body with no member keeps the snapshot; one valid member replaces it', async () => {
	await control('POST', '/test/family/self');
	await control('POST', '/test/probe/fresh?age_s=0');
	try {
		assert.notEqual(await mod.fetchProcessFamilySnapshot(), null);
		const kept = mod.readProcessFamilySnapshot();
		assert.ok(kept.members.length >= 2);
		await control('POST', '/test/drift-next?mode=empty-members');
		await mod.fetchProcessFamilySnapshot();
		assert.equal(mod.readProcessFamilySnapshot(), kept, 'empty-members: the snapshot is kept');
		assert.notEqual(
			mod.readFreshProcessFamilySnapshot(30_000),
			null,
			'empty-members: still current'
		);
		await control('POST', '/test/drift-next?mode=one-member');
		const replaced = await mod.fetchProcessFamilySnapshot();
		assert.equal(replaced.members.length, 1, 'one valid member replaces the snapshot');
		assert.notEqual(mod.readProcessFamilySnapshot(), kept);
	} finally {
		await control('POST', '/test/probe/captured');
	}
});

test('refreshes throttle on the last attempt, and a failed attempt is retried after the interval', async () => {
	const interval = 10_000;
	await control('POST', '/test/family/exited');
	await mod.fetchProcessFamilySnapshot();
	const now = performance.now();
	assert.equal(
		mod.processFamilyRefreshDue(interval, now),
		false,
		'unavailable: not again on the next tick'
	);
	assert.equal(
		mod.processFamilyRefreshDue(interval, now + interval),
		true,
		'unavailable: retried after the interval'
	);
});

// Last: it stops the server.
test('a failed request keeps the snapshot and is throttled, then retried', async () => {
	const interval = 10_000;
	await control('POST', '/test/family/self');
	await mod.fetchProcessFamilySnapshot();
	assert.notEqual(mod.readProcessFamilySnapshot(), null);

	await stopFixtureServer(serverProcess, apiBase);
	// The engine is gone: a real network failure, not a status code.
	await mod.fetchProcessFamilySnapshot();
	assert.notEqual(
		mod.readProcessFamilySnapshot(),
		null,
		'a failure says nothing about the processes'
	);
	assert.notEqual(mod.processFamilySnapshotAgeMs(), null);
	const now = performance.now();
	assert.equal(
		mod.processFamilyRefreshDue(interval, now),
		false,
		'failure: not again on the next tick'
	);
	assert.equal(
		mod.processFamilyRefreshDue(interval, now + interval),
		true,
		'failure: retried after the interval'
	);
});
