// Port of the tests in src-tauri/src/launch.rs. The flock is held by a real
// Python process making the same call apps/engine_core/lock.py makes.
import * as assert from 'node:assert/strict';
import { type ChildProcess, spawn } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { after, test } from 'node:test';

import { healthOk } from '../src/engine';
import { type LockProbe, flockProbe, inspectLock, makeLockProbe, parseLockJson, performancePageUrl, pidCanAct, readLockFields, readLockJsonWithAttempts, signalHolder, tryParseLockJson } from '../src/launch';
import { TaskkillUnavailableError } from '../src/engine';
import { HOLD_LOCK_SCRIPT, PYTHON, scratchDir, statusServer } from './helpers';

const probe = makeLockProbe(PYTHON);
const always = async (): Promise<boolean> => true;
const never = async (): Promise<boolean> => false;
const holders: ChildProcess[] = [];

after(() => {
	for (const holder of holders) holder.kill('SIGKILL');
});

/** Hold an exclusive flock on `lock` from another process; resolves once held. */
async function holdLock(lock: string, blob: string): Promise<ChildProcess> {
	fs.writeFileSync(lock, blob);
	const holder = spawn(
		PYTHON,
		['-c', HOLD_LOCK_SCRIPT, lock],
		{ stdio: ['ignore', 'pipe', 'inherit'] }
	);
	holders.push(holder);
	await new Promise<void>((resolve) => holder.stdout?.once('data', () => resolve()));
	return holder;
}

test('a missing lock file means spawn', async () => {
	const dir = scratchDir('launch-missing');
	assert.deepEqual(await inspectLock(path.join(dir, '.engine.lock'), always, probe), { kind: 'spawn' });
});

test('an unheld lock file means spawn, even when it names a live pid', async () => {
	const dir = scratchDir('launch-free');
	const lock = path.join(dir, '.engine.lock');
	fs.writeFileSync(lock, JSON.stringify({ pid: process.pid, host: '127.0.0.1', port: 9 }));
	assert.equal(flockProbe(PYTHON, lock)?.held, false);
	assert.deepEqual(await inspectLock(lock, always, probe), { kind: 'spawn' });
});

test('a held lock with a live, healthy holder means adopt', async () => {
	const dir = scratchDir('launch-adopt');
	const lock = path.join(dir, '.engine.lock');
	const server = await statusServer(200);
	try {
		const holder = await holdLock(lock, JSON.stringify({ pid: 0, host: '127.0.0.1', port: server.port }));
		// Name the holder itself, as the engine does.
		const blob = JSON.stringify({ pid: holder.pid, host: '127.0.0.1', port: server.port, heartbeat_at: '2099-01-01T00:00:00.000+00:00' });
		fs.writeFileSync(lock, blob);
		assert.equal(flockProbe(PYTHON, lock)?.held, true);
		assert.deepEqual(await inspectLock(lock, (port) => healthOk(port), probe), {
			kind: 'adopt',
			pid: holder.pid,
			host: '127.0.0.1',
			port: server.port
		});
	} finally {
		await server.close();
	}
});

test('a held lock whose holder is not answering health means stop-or-quit', async () => {
	const dir = scratchDir('launch-stop');
	const lock = path.join(dir, '.engine.lock');
	const holder = await holdLock(lock, '{}');
	fs.writeFileSync(lock, JSON.stringify({ pid: holder.pid, host: '127.0.0.1', port: 9 }));
	const plan = await inspectLock(lock, never, probe);
	assert.equal(plan.kind, 'stop-or-quit');
	assert.match((plan as { detail: string }).detail, /not answering health/);
});

test('a held lock naming a dead pid is never adopt', async () => {
	const dir = scratchDir('launch-dead');
	const lock = path.join(dir, '.engine.lock');
	await holdLock(lock, '{}');
	fs.writeFileSync(lock, JSON.stringify({ pid: 2 ** 22 + 7, host: '127.0.0.1', port: 9 }));
	const plan = await inspectLock(lock, always, probe);
	assert.equal(plan.kind, 'stop-or-quit');
	assert.match((plan as { detail: string }).detail, /dead or zombie pid/);
});

test('a held lock that names no pid is stop-or-quit with pid 0', async () => {
	const dir = scratchDir('launch-nopid');
	const lock = path.join(dir, '.engine.lock');
	await holdLock(lock, '{}');
	const plan = await inspectLock(lock, always, probe);
	assert.deepEqual(plan, { kind: 'stop-or-quit', pid: 0, detail: `engine lock ${lock} is held but does not name a pid` });
});

test('without the payload interpreter, pid liveness stands in for the flock', async () => {
	const dir = scratchDir('launch-fallback');
	const lock = path.join(dir, '.engine.lock');
	const fallback = makeLockProbe(path.join(dir, 'no-such-python'));
	fs.writeFileSync(lock, JSON.stringify({ pid: 2 ** 22 + 7, port: 9 }));
	assert.deepEqual(fallback(lock), { held: false, contents: fs.readFileSync(lock, 'utf8'), method: 'pid-liveness' });
	fs.writeFileSync(lock, JSON.stringify({ pid: process.pid, port: 9 }));
	assert.equal(fallback(lock)?.held, true);
	assert.equal(fallback(lock)?.method, 'pid-liveness');
});

test('pidCanAct rejects 0 and a dead pid, accepts this process', () => {
	assert.equal(pidCanAct(0), false);
	assert.equal(pidCanAct(2 ** 22 + 7), false);
	assert.equal(pidCanAct(process.pid), true);
});

test('lock JSON parsing ignores junk rather than guessing', () => {
	assert.deepEqual(parseLockJson('not json'), { pid: null, host: null, port: null });
	assert.deepEqual(parseLockJson('[1,2]'), { pid: null, host: null, port: null });
	assert.deepEqual(parseLockJson('{"pid":"12","port":-1}'), { pid: null, host: null, port: null });
	assert.deepEqual(parseLockJson('{"pid":12,"host":"127.0.0.1","port":8683}'), { pid: 12, host: '127.0.0.1', port: 8683 });
});

test('readLockFields needs both a pid and a port', () => {
	const dir = scratchDir('launch-fields');
	const lock = path.join(dir, '.engine.lock');
	fs.writeFileSync(lock, '{"pid":12}');
	assert.equal(readLockFields(lock), null);
	fs.writeFileSync(lock, '{"pid":12,"port":8683}');
	assert.deepEqual(readLockFields(lock), { pid: 12, port: 8683 });
});

test('readLockJsonWithAttempts retries unparsable bodies then succeeds', () => {
	const queue = ['   ', '{"pid":12,"port":8683}'];
	const holder = readLockJsonWithAttempts(() => queue.shift() ?? null);
	assert.deepEqual(holder, { pid: 12, host: null, port: 8683 });
});

test('readLockJsonWithAttempts returns null when the bound is exhausted', () => {
	assert.equal(readLockJsonWithAttempts(() => 'not-json'), null);
});

test('tryParseLockJson treats empty object as parsed, not unparsable', () => {
	assert.deepEqual(tryParseLockJson('{}'), { pid: null, host: null, port: null });
	assert.equal(tryParseLockJson('   '), null);
});

test('inspectLock on a held empty object does not wait the full retry bound', async () => {
	const dir = scratchDir('launch-nopid-fast');
	const lock = path.join(dir, '.engine.lock');
	fs.writeFileSync(lock, '{}');
	// A stub probe: the real one spawns python3, and that spawn was what the old
	// wall-clock check measured. This test is about the reader, not the spawn.
	const heldProbe = (): LockProbe => ({ held: true, contents: '{}', method: 'flock' });
	const started = Date.now();
	const plan = await inspectLock(lock, always, heldProbe);
	// Loaded-runner bound: local median is 14 ms (5 runs: 13, 14, 14, 13, 14); the old 40 ms bound took 430 ms
	// on PR #3807's loaded self-hosted runner (issue #8542). max(10 x median, 500 ms) = 500 ms.
	assert.equal(Date.now() - started < 500, true);
	assert.deepEqual(plan, { kind: 'stop-or-quit', pid: 0, detail: `engine lock ${lock} is held but does not name a pid` });
	// The retry bound is LOCK_READ_ATTEMPTS reads; a parsable first read must return
	// on attempt one. Counting reads asserts that without reading the clock.
	let reads = 0;
	const parsed = readLockJsonWithAttempts(() => {
		reads += 1;
		return '{}';
	});
	assert.notEqual(parsed, null);
	assert.equal(reads, 1, 'a parsable read was retried');
});

test('the performance page adds the frame probe only when OPENDJ_PERF_PROBE is 1', () => {
	const origin = 'http://engine.invalid';
	assert.equal(performancePageUrl(origin, { OPENDJ_PERF_PROBE: '1' }), `${origin}/performance?perfprobe=1`);
	assert.equal(performancePageUrl(origin, {}), `${origin}/performance`);
	assert.equal(performancePageUrl(origin, { OPENDJ_PERF_PROBE: '0' }), `${origin}/performance`);
});

test('an adopted Windows engine is stopped through the tree seam, for both signals', () => {
	const sent: Array<[number, NodeJS.Signals]> = [];
	const record = (pid: number, signal: NodeJS.Signals): boolean => (sent.push([pid, signal]), true);
	signalHolder(4242, 'SIGTERM', 'win32', record);
	signalHolder(4242, 'SIGKILL', 'win32', record);
	assert.deepEqual(sent, [[4242, 'SIGTERM'], [4242, 'SIGKILL']]);
});

test('an adopted Windows engine with no taskkill.exe does not throw out of quit', () => {
	const gone = 2 ** 22 - 1; // above any pid a test run reaches, so process.kill finds nothing
	assert.doesNotThrow(() =>
		signalHolder(gone, 'SIGTERM', 'win32', () => {
			throw new TaskkillUnavailableError('no taskkill.exe');
		})
	);
});

test('a failure that is not a missing taskkill.exe still surfaces', () => {
	assert.throws(
		() =>
			signalHolder(4242, 'SIGTERM', 'win32', () => {
				throw new RangeError('boom');
			}),
		/boom/
	);
});
