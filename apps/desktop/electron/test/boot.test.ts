// Port of start_engine / spawn_fresh_engine (main.rs), with real processes.
import * as assert from 'node:assert/strict';
import { type ChildProcess, spawn } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { after, test } from 'node:test';

import { type BootDeps, defaultBootDeps, startEngine } from '../src/boot';
import { makeLockProbe } from '../src/launch';
import { PARENT_FILE, shutdownSupervised } from '../src/supervisor';
import { fakePayload, HOLD_LOCK_SCRIPT, POSIX_ONLY, PYTHON, scratchDir } from './helpers';

const held: ChildProcess[] = [];
after(() => held.forEach((child) => child.kill('SIGKILL')));

function deps(answer: boolean, asked: string[] = []): BootDeps {
	return {
		...defaultBootDeps(makeLockProbe(PYTHON), async (pid, detail) => {
			asked.push(`${pid}: ${detail}`);
			return answer;
		}),
		bootTimeoutMs: 5_000
	};
}

test('no lock: a fresh engine is spawned and .engine.parent names this shell', { skip: POSIX_ONLY('owner-only permission bits (0o600)') }, async () => {
	const dir = scratchDir('boot-spawn');
	const data = path.join(dir, 'data');
	fs.mkdirSync(data, { recursive: true }); // main.ts creates it before boot
	const started = await startEngine(fakePayload(dir), data, path.join(data, 'logs/engine.log'), deps(false));
	try {
		assert.equal(started.kind, 'spawned');
		const parent = path.join(data, '.engine.parent');
		assert.equal(fs.readFileSync(parent, 'utf8'), `${process.pid}\n`);
		assert.equal(fs.statSync(parent).mode & 0o777, 0o600);
	} finally {
		await shutdownSupervised(started);
	}
});

test('a held lock the user declines to stop fails with the reason, spawning nothing', async () => {
	const dir = scratchDir('boot-decline');
	const data = path.join(dir, 'data');
	fs.mkdirSync(data, { recursive: true });
	const lock = path.join(data, '.engine.lock');
	fs.writeFileSync(lock, '{}');
	const holder = spawn(PYTHON, ['-c', HOLD_LOCK_SCRIPT, lock], {
		stdio: ['ignore', 'pipe', 'inherit']
	});
	held.push(holder);
	await new Promise((resolve) => holder.stdout?.once('data', resolve));
	fs.writeFileSync(lock, JSON.stringify({ pid: holder.pid, port: 9 }));
	const asked: string[] = [];
	await assert.rejects(startEngine(fakePayload(dir), data, path.join(data, 'logs/engine.log'), deps(false, asked)), /could not start/);
	assert.equal(asked.length, 1);
	assert.match(asked[0] as string, /not answering health/);
	assert.equal(fs.existsSync(path.join(data, 'logs/engine.log')), false, 'no engine was launched');
});

test('adopt health failure clears .engine.parent before surfacing the error', async () => {
	const dir = scratchDir('boot-adopt-health-fail');
	const data = path.join(dir, 'data');
	fs.mkdirSync(data, { recursive: true });
	const lock = path.join(data, '.engine.lock');
	fs.writeFileSync(lock, JSON.stringify({ pid: process.pid, port: 9, host: '127.0.0.1' }));
	let healthChecks = 0;
	const lockContents = fs.readFileSync(lock, 'utf8');
	await assert.rejects(
		startEngine(fakePayload(dir), data, path.join(data, 'logs/engine.log'), {
			...deps(false),
			lockProbe: () => ({ held: true, contents: lockContents, method: 'pid-liveness' }),
			health: async () => {
				healthChecks += 1;
				return healthChecks === 1;
			}
		}),
		/could not adopt/
	);
	assert.equal(fs.existsSync(path.join(data, PARENT_FILE)), false);
});

test('a boot failure carries the engine log tail into the dialog', async () => {
	const dir = scratchDir('boot-fail');
	const data = path.join(dir, 'data');
	fs.mkdirSync(data, { recursive: true });
	await assert.rejects(startEngine(fakePayload(dir, 'exit'), data, path.join(data, 'logs/engine.log'), deps(false)), (error: unknown) => {
		const detail = (error as { detail?: string }).detail ?? '';
		return /Last engine output:/.test(detail) && /fake-engine stderr line/.test(detail);
	});
});
