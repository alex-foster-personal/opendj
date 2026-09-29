// Port of the tests in src-tauri/src/launch.rs. The flock is held by a real
// Python process making the same call apps/engine_core/lock.py makes.
import * as assert from 'node:assert/strict';
import { type ChildProcess, spawn } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { after, test } from 'node:test';

import { healthOk } from '../src/engine';
import { flockProbe, inspectLock, makeLockProbe, parseLockJson, pidCanAct, readLockFields } from '../src/launch';
import { scratchDir, statusServer } from './helpers';

const PYTHON = 'python3';
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
		['-c', 'import fcntl,os,sys,time\nfd=os.open(sys.argv[1],os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\nprint("held",flush=True)\ntime.sleep(60)', lock],
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
