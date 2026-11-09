// The readiness launcher in midi-maps-test-server.mjs (issue #4332).
//
// Every case runs a REAL child process: the real midi_maps_test_server for
// the ready path, and small `node -e` programs for the failure paths, since
// the launcher's contract is about how any child behaves on the way to
// ready (exit early, stop making progress, keep progressing past the stall
// budget, never spawn at all).
//
// Regression lines:
//   if a child that exits before LISTENING is waited out instead of failing at once then broken
//   if a child that stops making progress is not failed after the stall budget then broken
//   if a child that keeps reporting progress is cut off by the stall budget then broken
//   if a failure message omits the exit status, the stages reached or the stderr tail then broken

import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

import { startMidiMapsTestServer } from './midi-maps-test-server.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPO_ROOT = resolve(FRONTEND_ROOT, '../../..');
const started = [];

after(() => {
	for (const proc of started) proc.kill();
});

function _node(script) {
	return { command: process.execPath, args: ['-e', script] };
}

test('the real server is ready only after its boot stages and a real GET', async () => {
	const server = await startMidiMapsTestServer({ cwd: REPO_ROOT });
	started.push(server.proc);
	assert.deepEqual(
		server.boot.map(({ stage }) => stage),
		['interpreter', 'imported', 'app_created', 'listening']
	);
	const response = await fetch(`${server.origin}/api/v1/midi/maps`);
	assert.equal(response.status, 200);
	assert.ok(server.readyAfterMs >= 0);
});

test('a child that exits before LISTENING fails at once with its status and stderr', async () => {
	const t0 = Date.now();
	await assert.rejects(
		startMidiMapsTestServer({
			cwd: REPO_ROOT,
			..._node(
				"console.log('BOOT interpreter 5'); console.error('ImportError: no module named demo'); process.exit(3)"
			),
			stallMs: 30_000,
			totalMs: 60_000
		}),
		(error) => {
			assert.match(error.message, /the process exited before it was ready/);
			assert.match(error.message, /exited with code 3 signal null/);
			assert.match(error.message, /Boot stages reached: interpreter@5ms/);
			assert.match(error.message, /ImportError: no module named demo/);
			return true;
		}
	);
	assert.ok(Date.now() - t0 < 10_000, 'an exit must not wait out the 30 s stall budget');
});

test('a child that exits 0 before LISTENING is a failure too, not a silent wait', async () => {
	await assert.rejects(
		startMidiMapsTestServer({
			cwd: REPO_ROOT,
			..._node('process.exit(0)'),
			stallMs: 30_000,
			totalMs: 60_000
		}),
		/Boot stages reached: none[\s\S]*exited with code 0 signal null/
	);
});

test('a child that stops making progress fails after the stall budget and is killed', async () => {
	await assert.rejects(
		startMidiMapsTestServer({
			cwd: REPO_ROOT,
			..._node(
				"console.log('BOOT interpreter 1'); console.log('BOOT imported 2'); setInterval(() => {}, 1000)"
			),
			stallMs: 400,
			totalMs: 30_000
		}),
		(error) => {
			assert.match(error.message, /no startup progress for 400ms after stage 'imported'/);
			assert.match(error.message, /still running as pid \d+, killed/);
			assert.match(error.message, /stall budget 400ms, ceiling 30000ms/);
			return true;
		}
	);
});

test('a child that keeps reporting progress is waited for past the stall budget, up to the ceiling', async () => {
	const t0 = Date.now();
	await assert.rejects(
		startMidiMapsTestServer({
			cwd: REPO_ROOT,
			..._node(
				"process.stdout.write('BOOT step0 100\\n'); let n = 0; setInterval(() => { n++; process.stdout.write(`BOOT step${n} ${(n + 1) * 100}\\n`); }, 100)"
			),
			stallMs: 2_000,
			totalMs: 5_000
		}),
		/not listening within the 5000ms ceiling/
	);
	assert.ok(Date.now() - t0 >= 4_900, 'progress lines must restart the stall budget');
});

test('a command that cannot be spawned fails with the spawn error', async () => {
	await assert.rejects(
		startMidiMapsTestServer({ cwd: REPO_ROOT, command: 'mdt-no-such-program-4332', args: [] }),
		/could not spawn 'mdt-no-such-program-4332'/
	);
});
