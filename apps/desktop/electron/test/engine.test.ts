// Port of the tests in src-tauri/src/engine.rs, against real processes.
import * as assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import * as fs from 'node:fs';
import * as net from 'node:net';
import * as path from 'node:path';
import { PassThrough } from 'node:stream';
import { test } from 'node:test';

import {
	APPSTORE_PROFILE,
	Engine,
	EngineError,
	LogSink,
	STRIPPED_ENV,
	buildProfileFor,
	freeLoopbackPort,
	healthOk,
	pumpStream,
	spawnEngine,
	stopProcessGroup
} from '../src/engine';
import { verifyLogWritable } from '../src/shell-log';
import { FAKE_ENGINE, fakePayload, pidGone, scratchDir, statusServer, waitFor } from './helpers';

// ----- build profile: both directions (SAND-04) ------------------------------
test('a container env var selects the appstore profile', () => {
	assert.equal(buildProfileFor('com.opendj.desktop', '/Users/dj', true), APPSTORE_PROFILE);
});

test('a container-shaped HOME alone selects the appstore profile', () => {
	assert.equal(buildProfileFor(undefined, '/Users/dj/Library/Containers/com.opendj.desktop/Data', true), APPSTORE_PROFILE);
});

test('an unsandboxed shell passes no profile (over-detection control)', () => {
	assert.equal(buildProfileFor(undefined, '/Users/dj', true), null);
	assert.equal(buildProfileFor(undefined, undefined, true), null);
});

test('a blank container id is not a container', () => {
	assert.equal(buildProfileFor('', '/Users/dj', true), null);
	assert.equal(buildProfileFor('   ', '/Users/dj', true), null);
});

test('a HOME merely containing Library is not a container', () => {
	assert.equal(buildProfileFor(undefined, '/Users/dj/Library/Application Support', true), null);
});

test('the sandbox is macOS only', () => {
	assert.equal(buildProfileFor('com.opendj.desktop', '/Users/dj/Library/Containers/x/Data', false), null);
});

// ----- log pump ------------------------------------------------------------
test('the pump copies every chunk and resolves at end of stream', async () => {
	const dir = scratchDir('pump');
	const sink = new LogSink(path.join(dir, 'engine.log'));
	const stream = new PassThrough();
	const done = pumpStream(stream, 'stdout', sink);
	stream.write('first\n');
	stream.write('second\n');
	stream.end();
	await done;
	assert.equal(fs.readFileSync(sink.path, 'utf8'), 'first\nsecond\n');
	assert.equal(sink.failure(), null);
});

test('a terminal read error is reported, not swallowed', async () => {
	const dir = scratchDir('pump-read-error');
	const sink = new LogSink(path.join(dir, 'engine.log'));
	const stream = new PassThrough();
	const done = pumpStream(stream, 'stderr', sink);
	stream.write('before\n');
	stream.destroy(Object.assign(new Error('broken pipe'), { code: 'EPIPE' }));
	await assert.rejects(done, /could not read engine stderr/);
});

test('an unwritable log is refused before the engine exists, and a pump into it fails', async () => {
	const dir = scratchDir('pump-unwritable');
	const log = path.join(dir, 'engine.log');
	fs.mkdirSync(log); // a directory where the log belongs
	assert.throws(() => verifyLogWritable(log), (error: unknown) => {
		return error instanceof EngineError && /could not write its engine log/.test(error.headline);
	});
	const stream = new PassThrough();
	const done = pumpStream(stream, 'stdout', new LogSink(log));
	stream.end('line\n');
	await assert.rejects(done, /could not write engine stdout/);
});

// ----- process group -------------------------------------------------------
test('stopping the group terminates the engine', async () => {
	const child = spawn('/bin/sleep', ['30'], { detached: true, stdio: 'ignore' });
	const exited = new Promise((resolve) => child.once('exit', resolve));
	stopProcessGroup(child.pid as number);
	const outcome = await Promise.race([exited.then(() => 'exited'), new Promise((r) => setTimeout(() => r('timeout'), 5_000))]);
	if (outcome !== 'exited') child.kill('SIGKILL');
	assert.equal(outcome, 'exited');
});

test('shutdown waits out the SIGTERM grace, then escalates to SIGKILL', async () => {
	const dir = scratchDir('shutdown-escalate');
	const port = await freeLoopbackPort();
	const engine = spawnEngine(fakePayload(dir, 'ignore-term'), path.join(dir, 'data'), path.join(dir, 'data/logs/engine.log'), port);
	await engine.waitUntilHealthy(10_000);
	const started = Date.now();
	await engine.shutdown();
	const elapsed = Date.now() - started;
	assert.ok(elapsed >= 5_000, `must wait the 5 s grace before SIGKILL, took ${elapsed} ms`);
	assert.ok(elapsed < 10_000, `SIGKILL should end it promptly, took ${elapsed} ms`);
	assert.equal(engine.tryReap()?.signal, 'SIGKILL');
});

test('an engine that honors SIGTERM is not escalated (the opposite direction)', async () => {
	const dir = scratchDir('shutdown-graceful');
	const port = await freeLoopbackPort();
	const engine = spawnEngine(fakePayload(dir), path.join(dir, 'data'), path.join(dir, 'data/logs/engine.log'), port);
	await engine.waitUntilHealthy(10_000);
	const started = Date.now();
	await engine.shutdown();
	assert.ok(Date.now() - started < 5_000);
	assert.equal(engine.tryReap()?.signal, 'SIGTERM');
});

test('a lost log fails the launch instead of reporting healthy', async () => {
	const dir = scratchDir('lost-log');
	const child = spawn('/bin/sleep', ['30'], { detached: true, stdio: 'ignore' });
	const sink = new LogSink(path.join(dir, 'engine.log'));
	sink.recordFailure('could not write engine stdout: disk full');
	// Port 1: nothing listens, so a healthy verdict could only come from skipping the check.
	const engine = new Engine(child, 1, dir, sink.path, sink);
	await assert.rejects(engine.waitUntilHealthy(30_000), (error: unknown) => {
		return error instanceof EngineError && /lost the engine log/.test(error.headline) && /disk full/.test(error.detail);
	});
	await engine.shutdown();
});

// ----- health ----------------------------------------------------------------
test('health is true for a local 200 and false for a 404 or a closed port', async () => {
	const ok = await statusServer(200);
	const missing = await statusServer(404);
	const closed = await freeLoopbackPort();
	try {
		assert.equal(await healthOk(ok.port), true);
		assert.equal(await healthOk(missing.port), false);
		assert.equal(await healthOk(closed), false);
	} finally {
		await ok.close();
		await missing.close();
	}
});

test('health ignores proxy settings (it is a raw loopback socket)', async () => {
	const ok = await statusServer(200);
	const saved = process.env.HTTP_PROXY;
	process.env.HTTP_PROXY = 'http://127.0.0.1:1';
	try {
		assert.equal(await healthOk(ok.port), true);
	} finally {
		if (saved === undefined) delete process.env.HTTP_PROXY;
		else process.env.HTTP_PROXY = saved;
		await ok.close();
	}
});

test('freeLoopbackPort hands out a port that can be bound right away', async () => {
	const port = await freeLoopbackPort();
	await new Promise<void>((resolve, reject) => {
		const server = net.createServer().once('error', reject).listen(port, '127.0.0.1', () => server.close(() => resolve()));
	});
});

// ----- spawn contract ----------------------------------------------------------
test('spawn passes the launch contract and strips inherited env', async () => {
	const dir = scratchDir('spawn-env');
	const data = path.join(dir, 'data');
	const log = path.join(data, 'logs/engine.log');
	for (const name of STRIPPED_ENV) process.env[name] = 'from-a-developer-terminal';
	let engine: Engine;
	try {
		engine = spawnEngine(fakePayload(dir), data, log, await freeLoopbackPort());
	} finally {
		for (const name of STRIPPED_ENV) delete process.env[name];
	}
	await engine.waitUntilHealthy(10_000);
	await waitFor(() => fs.existsSync(log) && fs.readFileSync(log, 'utf8').includes('fake-engine stderr line'));
	const text = fs.readFileSync(log, 'utf8');
	assert.match(text, new RegExp(`"--data-dir","${data.replace(/[/\\]/g, '\\$&')}","--host","127.0.0.1","--port","${engine.port}"`));
	for (const name of STRIPPED_ENV) assert.match(text, new RegExp(`env ${name}=<unset>`));
	assert.match(text, new RegExp(`env OPENDJ_PARENT_PID=${process.pid}\\n`));
	assert.match(text, /env OPENDJ_ENGINE_WARN_LOG=.*logs\/engine-warn\.log/);
	assert.match(text, /env OPENDJ_ENGINE_LOG_BOOT_ID=shell-\d+-\d+/);
	assert.match(text, /fake-engine stderr line/, 'stderr is pumped into the same log');
	await engine.shutdown();
	assert.ok(await waitFor(() => pidGone(engine.pid)));
});

test('an engine that exits during boot is named with its status', async () => {
	const dir = scratchDir('spawn-exit');
	const engine = spawnEngine(fakePayload(dir, 'exit'), path.join(dir, 'data'), path.join(dir, 'data/logs/engine.log'), await freeLoopbackPort());
	await assert.rejects(engine.waitUntilHealthy(10_000), /stopped while starting up[\s\S]*exit status 3/);
});

test('an engine that never answers times out with the address it tried', async () => {
	const dir = scratchDir('spawn-timeout');
	const port = await freeLoopbackPort();
	const engine = spawnEngine(fakePayload(dir, 'never-ready'), path.join(dir, 'data'), path.join(dir, 'data/logs/engine.log'), port);
	await assert.rejects(engine.waitUntilHealthy(1_000), new RegExp(`did not answer http://127.0.0.1:${port}/api/v1/health within 1s`));
	await engine.shutdown();
});

test('a payload without its launcher is refused before any spawn', () => {
	const dir = scratchDir('spawn-missing');
	assert.throws(
		() => spawnEngine(path.join(dir, 'payload'), path.join(dir, 'data'), path.join(dir, 'data/logs/engine.log'), 1),
		/has no engine inside it/
	);
	assert.equal(fs.existsSync(path.join(dir, 'data')), false, 'nothing was created for a build that cannot boot');
});

test('the fake engine fixture exists and is executable', () => {
	fs.accessSync(FAKE_ENGINE, fs.constants.X_OK);
});
