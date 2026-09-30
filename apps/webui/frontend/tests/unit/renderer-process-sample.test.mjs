import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const sample = await loadTypeScriptModule('tests/e2e/support/renderer-process-sample.ts');

test('selectChromiumFamilyPids takes every process, not the first renderer', () => {
	// Seen live Fri 25 Sep 2026: three renderers and no gpu-process; the first
	// renderer (~72 MB) was not the page's own (~150-250 MB).
	const pids = sample.selectChromiumFamilyPids([
		{ id: 111, type: 'browser' },
		{ id: 222, type: 'renderer' },
		{ id: 333, type: 'renderer' },
		{ id: 444, type: 'utility' },
		{ id: 555, type: 'gpu-process' }
	]);
	assert.deepEqual(pids, [111, 222, 333, 444, 555]);
});

test('selectChromiumFamilyPids throws when no renderer is listed', () => {
	assert.throws(() => sample.selectChromiumFamilyPids([{ id: 1, type: 'browser' }]), /renderer/);
});

test('selectChromiumFamilyPids throws on a non-integer pid rather than dropping it', () => {
	assert.throws(
		() => sample.selectChromiumFamilyPids([{ id: 'x', type: 'renderer' }]),
		/pid/
	);
});

//-----------------------------------------------------------------------------
// PERFMODE-14 (issue #3960): this app's family is a pid tree, not a name match
//-----------------------------------------------------------------------------

/** Shape seen live on silver Fri 25 Sep 2026: this capture's engine (8726)
 * with a stem worker, plus two OTHER worktrees' engines named exactly like it. */
const PS_ROWS = [
	{ pid: 1, ppid: 0, rss_kb: 10, cpu_percent: 0 },
	{ pid: 500, ppid: 1, rss_kb: 80_000, cpu_percent: 1 }, // this engine
	{ pid: 501, ppid: 500, rss_kb: 40_000, cpu_percent: 2 }, // its stem worker
	{ pid: 502, ppid: 501, rss_kb: 1_000, cpu_percent: 0 }, // grandchild
	{ pid: 900, ppid: 1, rss_kb: 68_000, cpu_percent: 0 }, // another worktree's opendj-engine
	{ pid: 901, ppid: 1, rss_kb: 40_000, cpu_percent: 0 } // and another
];

test('descendantFamilyPids counts this engine and every descendant', () => {
	assert.deepEqual(sample.descendantFamilyPids(PS_ROWS, [500]), [500, 501, 502]);
});

test('descendantFamilyPids never counts a foreign engine, whatever it is named', () => {
	const family = sample.descendantFamilyPids(PS_ROWS, [500]);
	for (const foreign of [900, 901]) {
		assert.ok(
			!family.includes(foreign),
			`if pid ${foreign} is counted then another worktree's engine inflates both modes and drags the ratio toward 1`
		);
	}
});

test('descendantFamilyPids throws on a root that is not running, rather than an empty family', () => {
	assert.throws(() => sample.descendantFamilyPids(PS_ROWS, [4242]), /not running/);
	assert.throws(() => sample.descendantFamilyPids(PS_ROWS, []), /at least one root/);
});

//-----------------------------------------------------------------------------
// assertEngineOwnsListener (Sol P1/BLOCKING twice, PR #4034,
// discussion_r4137393523 then discussion_r4137872466): a local SSH/TCP
// forward to a remote engine passes the HTTP identity checks (they only read
// the response body) while lsof resolves the FORWARDER's pid, not the
// engine's. A deny-list of known forwarder commands was rejected as a second
// pass: an unlisted forwarder (kubectl port-forward, nc, a bespoke proxy)
// would still pass it. This is a POSITIVE check instead: the engine's own
// self-reported pid (from build-info) must be one of the pids actually
// listening locally.
//-----------------------------------------------------------------------------

test('assertEngineOwnsListener passes when the engine is one of the local listeners', () => {
	assert.doesNotThrow(() => sample.assertEngineOwnsListener([500, 501], 500));
});

test('assertEngineOwnsListener rejects an engine pid that is not among the local listeners', () => {
	// This is exactly what a local SSH/TCP forward produces: lsof resolves
	// the forwarder's own local pid (500), never the remote engine's
	// self-reported pid (99999), whatever the forwarder is called.
	assert.throws(
		() => sample.assertEngineOwnsListener([500], 99999),
		/pids listening on its port are \[500\]/
	);
});

// Codex P1/BLOCKING (PR #4034, discussion_r4137567525): a remote --engine's
// self-reported pid could coincidentally equal an unrelated LOCAL listener's
// pid on the same port number, which assertEngineOwnsListener alone cannot
// tell apart from the real thing. Refuse non-loopback origins outright.
test('assertLoopbackOrigin passes for localhost and loopback addresses', () => {
	for (const host of ['localhost', '127.0.0.1', '[::1]']) {
		assert.doesNotThrow(() => sample.assertLoopbackOrigin(`http://${host}:8686`));
	}
});

test('assertLoopbackOrigin rejects a remote hostname', () => {
	assert.throws(
		() => sample.assertLoopbackOrigin('http://box.example-tailnet.ts.net:8686'),
		/not loopback/
	);
});

// isEngineCommand / assertLocalProcessIsTheEngine (Sol P1/BLOCKING,
// discussion at sha=630cec1cce): pids are host-local integers with no
// cross-host uniqueness guarantee, so an ssh -L tunnel's local forwarder pid
// could numerically coincide with a remote engine's self-reported pid, which
// assertEngineOwnsListener alone cannot tell apart from the real thing. This
// checks the verified-local pid's OWN command line instead.
test('isEngineCommand recognizes the repo engine invocation and the packaged launcher', () => {
	assert.equal(sample.isEngineCommand('uv run --no-sync python -m apps.engine_core serve --port 8686'), true);
	assert.equal(
		sample.isEngineCommand('/Applications/OpenDJ.app/Contents/Resources/bin/opendj-engine'),
		true
	);
});

test('isEngineCommand rejects a forwarder, whatever it is called', () => {
	for (const command of ['ssh -L 8686:remote:8686 user@remote', 'kubectl port-forward pod/engine 8686:8686', 'socat TCP-LISTEN:8686 TCP:remote:8686']) {
		assert.equal(sample.isEngineCommand(command), false);
	}
});

// Codex P1/BLOCKING (PR #4034, discussion at sha=09612c7a6f): a substring
// match accepted a command that merely CONTAINS the engine's name without
// being it -- exactly the pid-coincidence case this function exists to
// close, since a forwarder passes just as easily as the real engine would.
test('isEngineCommand rejects a lookalike binary name, not just a known forwarder', () => {
	assert.equal(sample.isEngineCommand('/usr/local/bin/opendj-engine-proxy'), false);
});

test('isEngineCommand rejects a forwarder whose ARGUMENT happens to embed the engine name', () => {
	assert.equal(
		sample.isEngineCommand('ssh -L 8686:opendj-engine-host:8686 user@opendj-engine-host'),
		false
	);
});

// Sol P1/BLOCKING (PR #4034, discussion_r4148668247): the `-m
// apps.engine_core` adjacent-pair check searched ALL tokens, so a forwarder
// whose remote command happens to embed that exact module pair -- not just
// the engine's name as a substring, the FULL real invocation -- still
// passed, because nothing required argv[0] to actually be the interpreter
// running it.
test('isEngineCommand rejects an SSH forwarder whose remote command embeds the real module invocation', () => {
	assert.equal(sample.isEngineCommand('ssh -L 8686:remote:8686 host python -m apps.engine_core'), false);
});

// assertEnginePidPinned (Sol P1/BLOCKING, PR #4034, discussion_r4138402621):
// engineRootPids re-resolves the engine's pid fresh on every dwell tick, so a
// same-build engine restart mid-capture would otherwise pass every other
// identity check again and silently swap the measured process.
test('assertEnginePidPinned allows the first tick, when nothing is pinned yet', () => {
	assert.doesNotThrow(() => sample.assertEnginePidPinned(undefined, 500));
});

test('assertEnginePidPinned allows a later tick that matches the pinned pid', () => {
	assert.doesNotThrow(() => sample.assertEnginePidPinned(500, 500));
});

test('assertEnginePidPinned rejects a later tick whose engine pid changed', () => {
	assert.throws(() => sample.assertEnginePidPinned(500, 501), /pinned 500, now 501/);
});

// Codex P1/BLOCKING (PR #4034, discussion_r4138473507): dwellSample's catch
// block budgets an ordinary sample failure against the minimum-sample floor,
// which let a late-dwell engine restart (after the floor was already met by
// earlier, pre-restart samples) pass anyway. It must instead recognize this
// ONE error type and rethrow rather than budget it -- EnginePidMismatchError
// exists specifically so that check can be an `instanceof`, not a string
// match on the message.
test('a pid mismatch throws EnginePidMismatchError specifically, not a generic Error', () => {
	assert.throws(() => sample.assertEnginePidPinned(500, 501), sample.EnginePidMismatchError);
	try {
		sample.assertEnginePidPinned(500, 501);
		assert.fail('expected assertEnginePidPinned to throw');
	} catch (error) {
		assert.ok(error instanceof sample.EnginePidMismatchError);
		assert.ok(error instanceof Error);
	}
});

test('assertLocalProcessIsTheEngine rejects a real local pid that is not the engine', () => {
	// This test's own process is a real, running, local pid -- and it is
	// node, not the engine, so this is a genuine negative case, not a mock.
	assert.throws(
		() => sample.assertLocalProcessIsTheEngine(process.pid),
		/does not name the engine/
	);
});

test('parsePsTable parses rows and throws on a malformed one rather than dropping it', () => {
	assert.deepEqual(sample.parsePsTable('  500     1  80000   1.5\n  501   500  40000   0.0\n'), [
		{ pid: 500, ppid: 1, rss_kb: 80000, cpu_percent: 1.5 },
		{ pid: 501, ppid: 500, rss_kb: 40000, cpu_percent: 0 }
	]);
	assert.throws(() => sample.parsePsTable('500 1 abc 0.0'), /unparseable/);
	assert.throws(() => sample.parsePsTable(''), /no rows/);
});

test('parseFootprintMb reads phys_footprint in every unit footprint prints', () => {
	const line = (unit) => `Google Chrome Helper (Renderer) [432]: 64-bit    Footprint: 12 ${unit} (16384 bytes per page)`;
	assert.equal(sample.parseFootprintMb(line('MB'), 432), 12);
	assert.equal(sample.parseFootprintMb(line('GB'), 432), 12 * 1024);
	assert.equal(sample.parseFootprintMb(line('KB'), 432), 12 / 1024);
});

test('parseFootprintMb throws on output for a different pid or no Footprint line', () => {
	assert.throws(
		() => sample.parseFootprintMb('python3.12 [77]: 64-bit    Footprint: 90 MB (16384 bytes per page)', 78),
		/asked for 78/
	);
	assert.throws(() => sample.parseFootprintMb('footprint: no process found', 78), /no Footprint line/);
});

test('countLiveFamilyMembers counts live members only', () => {
	const count = sample.countLiveFamilyMembers({
		available: true,
		members: [
			{ name: 'opendj-engine', rss_mb: 50, source: 'live' },
			{ name: 'unnamed', source: 'live' },
			{ name: 'unnamed', physical_footprint_mb: 2.7, source: 'probe_log' }
		]
	});
	assert.equal(count, 2);
});

test('countLiveFamilyMembers throws on unavailable telemetry or an unknown source', () => {
	assert.throws(() => sample.countLiveFamilyMembers({ available: false }), /available/);
	assert.throws(
		() => sample.countLiveFamilyMembers({ available: true, members: [{ name: 'engine' }] }),
		/source/
	);
});
