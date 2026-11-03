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
	AUDIO_ENGINE_BIN,
	Engine,
	EngineError,
	LogSink,
	SHUTDOWN_GRACE_MS,
	STRIPPED_ENV,
	audioEngineEnv,
	buildProfileFor,
	freeLoopbackPort,
	healthOk,
	pickLoopbackPort,
	pumpStream,
	spawnEngine,
	signalGroup,
	spawnProcess,
	stopProcessGroup,
	sweepOrphanedGroup,
	TaskkillUnavailableError,
	EngineCleanupError,
	windowsTaskkill
} from '../src/engine';
import { stopHolderPid } from '../src/launch';
import { appendShellLog, installShellLogging, verifyLogWritable } from '../src/shell-log';
import { FAKE_ENGINE, fakePayload, pidGone, scratchDir, statusServer, waitFor } from './helpers';

// Port-picker decisions go to the shell log, as in the Tauri shell.
const portPickerLog = path.join(scratchDir('port-picker'), 'engine.log');
installShellLogging(portPickerLog);

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
	const engine = spawnEngine(fakePayload(dir, 'ignore-term'), path.join(dir, 'data'), path.join(dir, '(private path)'), port);
	await engine.waitUntilHealthy(10_000);
	const started = Date.now();
	await engine.shutdown(2_000);
	const elapsed = Date.now() - started;
	assert.ok(elapsed >= 2_000, `must wait the 2 s grace it was given before SIGKILL, took ${elapsed} ms`);
	assert.ok(elapsed < 7_000, `SIGKILL should end it promptly, took ${elapsed} ms`);
	assert.equal(engine.tryReap()?.signal, 'SIGKILL');
});

// - if the default grace drops below the Tauri shell's 25 s then a quit
//   SIGKILLs the engine mid-reap and its job workers outlive the app -> broken
test('the default stop grace is the Tauri shell\'s 25 s', () => {
	assert.equal(SHUTDOWN_GRACE_MS, 25_000);
});

test('an engine that honors SIGTERM is not escalated (the opposite direction)', async () => {
	const dir = scratchDir('shutdown-graceful');
	const port = await freeLoopbackPort();
	const engine = spawnEngine(fakePayload(dir), path.join(dir, 'data'), path.join(dir, '(private path)'), port);
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

test('a free preferred port is returned as is', async () => {
	const port = await freeLoopbackPort();
	assert.equal(await pickLoopbackPort(port), port);
	// The control line proves this file is the shell log. Absence of a
	// preferred-port line is then a real measurement, not an empty file.
	appendShellLog('INFO', `port-picker control ${port}`);
	const text = fs.readFileSync(portPickerLog, 'utf8');
	assert.match(text, new RegExp(`\\[shell INFO\\] port-picker control ${port}\\n`));
	assert.equal(text.includes(`preferred engine port ${port} is in use`), false);
});

test('a preferred port held on 127.0.0.1 falls back to a bindable port', async () => {
	const holder = net.createServer();
	await new Promise<void>((resolve, reject) => {
		holder.once('error', reject).listen(0, '127.0.0.1', () => resolve());
	});
	const address = holder.address();
	assert.ok(address !== null && typeof address !== 'string');
	const held = address.port;
	try {
		const picked = await pickLoopbackPort(held);
		assert.notEqual(picked, held);
		const text = fs.readFileSync(portPickerLog, 'utf8');
		const line = `[shell INFO] preferred engine port ${held} is in use; using OS-assigned port ${picked}\n`;
		assert.ok(text.includes(line), `shell log missing fallback line:\n${text}`);
		await new Promise<void>((resolve, reject) => {
			const server = net.createServer();
			server.once('error', reject).listen(picked, '127.0.0.1', () => server.close(() => resolve()));
		});
	} finally {
		await new Promise<void>((resolve) => holder.close(() => resolve()));
	}
});

test('a preferred port held only on ::1 is not free', async (t) => {
	const holder = net.createServer();
	try {
		await new Promise<void>((resolve, reject) => {
			holder.once('error', reject).listen(0, '::1', () => resolve());
		});
	} catch {
		t.skip('no IPv6 loopback on this host');
		return;
	}
	const address = holder.address();
	assert.ok(address !== null && typeof address !== 'string');
	const held = address.port;
	try {
		assert.notEqual(await pickLoopbackPort(held), held);
	} finally {
		await new Promise<void>((resolve) => holder.close(() => resolve()));
	}
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
	try {
		await engine.waitUntilHealthy(10_000);
		await waitFor(() => fs.existsSync(log) && fs.readFileSync(log, 'utf8').includes('fake-engine stderr line'));
		const text = fs.readFileSync(log, 'utf8');
		assert.match(text, new RegExp(`"--data-dir","${data.replace(/[/\\]/g, '\\$&')}","--host","127.0.0.1","--port","${engine.port}"`));
		for (const name of STRIPPED_ENV) assert.match(text, new RegExp(`env ${name}=<unset>`));
		assert.match(text, new RegExp(`env OPENDJ_PARENT_PID=${process.pid}\\n`));
		assert.match(text, /env OPENDJ_ENGINE_WARN_LOG=.*logs\/engine-warn\.log/);
		assert.match(text, /env OPENDJ_ENGINE_LOG_BOOT_ID=shell-\d+-\d+/);
		assert.match(text, /fake-engine stderr line/, 'stderr is pumped into the same log');
	} finally {
		await engine.shutdown();
	}
	assert.ok(await waitFor(() => pidGone(engine.pid)));
});

// ----- the Rust audio engine's binary (ADR NEW rust-audio-engine-process, D5) --
test('a bundled audio engine is handed to the engine as ODJ_AUDIO_BIN', async () => {
	const dir = scratchDir('spawn-audio-bin');
	const payload = fakePayload(dir);
	const binary = path.join(payload, AUDIO_ENGINE_BIN);
	fs.writeFileSync(binary, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
	const data = path.join(dir, 'data');
	const log = path.join(data, 'logs/engine.log');
	process.env.ODJ_AUDIO_BIN = '/Users/dev/music-dj-tools/apps/audio-engine/target/debug/odj-audio';
	let engine: Engine;
	try {
		engine = spawnEngine(payload, data, log, await freeLoopbackPort());
	} finally {
		delete process.env.ODJ_AUDIO_BIN;
	}
	try {
		await engine.waitUntilHealthy(10_000);
		await waitFor(() => fs.existsSync(log) && fs.readFileSync(log, 'utf8').includes('fake-engine stderr line'));
		assert.match(fs.readFileSync(log, 'utf8'), new RegExp(`env ODJ_AUDIO_BIN=${binary.replace(/[/\\.]/g, '\\$&')}\\n`));
	} finally {
		await engine.shutdown();
	}
});

test('no bundled audio engine: ODJ_AUDIO_BIN is unset, even when the terminal set one', async () => {
	const dir = scratchDir('spawn-no-audio-bin');
	const data = path.join(dir, 'data');
	const log = path.join(data, 'logs/engine.log');
	process.env.ODJ_AUDIO_BIN = '/Users/dev/music-dj-tools/apps/audio-engine/target/debug/odj-audio';
	let engine: Engine;
	try {
		engine = spawnEngine(fakePayload(dir), data, log, await freeLoopbackPort());
	} finally {
		delete process.env.ODJ_AUDIO_BIN;
	}
	try {
		await engine.waitUntilHealthy(10_000);
		await waitFor(() => fs.existsSync(log) && fs.readFileSync(log, 'utf8').includes('fake-engine stderr line'));
		assert.match(fs.readFileSync(log, 'utf8'), /env ODJ_AUDIO_BIN=<unset>\n/);
	} finally {
		await engine.shutdown();
	}
});

test('a non-executable audio engine file is not handed over', () => {
	const dir = scratchDir('audio-bin-mode');
	fs.mkdirSync(path.join(dir, 'bin'), { recursive: true });
	fs.writeFileSync(path.join(dir, AUDIO_ENGINE_BIN), 'not a program', { mode: 0o644 });
	assert.deepEqual(audioEngineEnv(dir, 'linux'), {});
	fs.chmodSync(path.join(dir, AUDIO_ENGINE_BIN), 0o755);
	assert.deepEqual(audioEngineEnv(dir, 'linux'), { ODJ_AUDIO_BIN: path.join(dir, AUDIO_ENGINE_BIN) });
});

test('an engine that exits during boot is named with its status', async () => {
	const dir = scratchDir('spawn-exit');
	const engine = spawnEngine(fakePayload(dir, 'exit'), path.join(dir, 'data'), path.join(dir, '(private path)'), await freeLoopbackPort());
	await assert.rejects(engine.waitUntilHealthy(10_000), /stopped while starting up[\s\S]*exit status 3/);
});

test('an engine that never answers times out with the address it tried', async () => {
	const dir = scratchDir('spawn-timeout');
	const port = await freeLoopbackPort();
	const engine = spawnEngine(fakePayload(dir, 'never-ready'), path.join(dir, 'data'), path.join(dir, '(private path)'), port);
	await assert.rejects(engine.waitUntilHealthy(1_000), new RegExp(`did not answer http://127.0.0.1:${port}/api/v1/health within 1s`));
	await engine.shutdown();
});

test('a payload without its launcher is refused before any spawn', () => {
	const dir = scratchDir('spawn-missing');
	assert.throws(
		() => spawnEngine(path.join(dir, 'payload'), path.join(dir, 'data'), path.join(dir, '(private path)'), 1),
		/has no engine inside it/
	);
	assert.equal(fs.existsSync(path.join(dir, 'data')), false, 'nothing was created for a build that cannot boot');
});

test('the fake engine fixture exists and is executable', () => {
	fs.accessSync(FAKE_ENGINE, fs.constants.X_OK);
});

test('taskkill is resolved under the system root, never looked up on PATH (Codex on #6830)', () => {
	const seen: string[] = [];
	const present = (file: string): boolean => (seen.push(file), true);
	assert.equal(
		windowsTaskkill({ SystemRoot: 'C:\\Windows', PATH: 'C:\\evil' }, present),
		'C:\\Windows\\System32\\taskkill.exe'
	);
	assert.equal(windowsTaskkill({ windir: 'D:\\WINNT' }, present), 'D:\\WINNT\\System32\\taskkill.exe');
	assert.deepEqual(seen, ['C:\\Windows\\System32\\taskkill.exe', 'D:\\WINNT\\System32\\taskkill.exe']);
});

test('no absolute system root, or no taskkill.exe there, resolves to nothing rather than a PATH lookup', () => {
	const present = (): boolean => true;
	assert.equal(windowsTaskkill({ PATH: 'C:\\Windows\\System32' }, present), null);
	assert.equal(windowsTaskkill({ SystemRoot: 'Windows' }, present), null);
	assert.equal(windowsTaskkill({ SystemRoot: 'C:\\Windows' }, () => false), null);
});

test('no taskkill.exe on Windows throws a distinct error, never "nothing was there" (Sol P1 on #6830)', () => {
	assert.throws(() => signalGroup(4242, 'SIGTERM', 'win32', () => null), TaskkillUnavailableError);
	assert.throws(() => signalGroup(4242, 'SIGKILL', 'win32', () => null), /no taskkill\.exe/);
});

function sleeper(name: string): { engine: Engine; calls: string[] } {
	const dir = scratchDir(name);
	// Node itself, so the Windows taskkill paths these tests play can also run on Windows (Sol P1 on #6830).
	const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 30_000)'], { detached: true, stdio: 'ignore' });
	const sink = new LogSink(path.join(dir, 'engine.log'));
	return { engine: new Engine(child, 1, dir, sink.path, sink), calls: [] };
}

test('a taskkill /T that fails goes straight to /F, and one that fails again kills the leader alone (Sol P1 on #6830)', async () => {
	const { engine, calls } = sleeper('taskkill-fails');
	engine.treeSignals = { platform: 'win32', send: (_pid, signal) => (calls.push(signal), false) };
	const started = Date.now();
	await assert.rejects(engine.shutdown(), EngineCleanupError);
	assert.deepEqual(calls, ['SIGTERM', 'SIGKILL']);
	assert.equal(engine.tryReap()?.signal, 'SIGKILL');
	assert.ok(Date.now() - started < 4_000, 'no grace is waited on a SIGTERM that never reached the tree');
});

test('no taskkill.exe at shutdown kills the leader alone without a second try', async () => {
	const { engine, calls } = sleeper('taskkill-missing');
	engine.treeSignals = {
		platform: 'win32',
		send: (_pid, signal) => {
			calls.push(signal);
			throw new TaskkillUnavailableError('no taskkill.exe');
		}
	};
	await assert.rejects(engine.shutdown(), EngineCleanupError);
	assert.deepEqual(calls, ['SIGTERM']);
	assert.equal(engine.tryReap()?.signal, 'SIGKILL');
});

test('control: a taskkill /T that succeeds is waited on as a graceful stop', async () => {
	const { engine, calls } = sleeper('taskkill-ok');
	engine.treeSignals = {
		platform: 'win32',
		send: (pid, signal) => {
			calls.push(signal);
			process.kill(pid, signal);
			return true;
		}
	};
	await engine.shutdown();
	assert.deepEqual(calls, ['SIGTERM']);
	assert.equal(engine.tryReap()?.signal, 'SIGTERM');
});

// ----- orphans of an exited leader (EL-17-F9, #7127) ---------------------------
const POSIX_GROUPS = process.platform === 'win32' && 'process groups are POSIX only';

async function survivorOf(pidFile: string): Promise<number> {
	await waitFor(() => fs.existsSync(pidFile) && fs.readFileSync(pidFile, 'utf8').trim() !== '', 10_000);
	return Number(fs.readFileSync(pidFile, 'utf8').trim());
}

function spawnGroupFixture(label: string, mode: string, env: Record<string, string>): { engine: ReturnType<typeof spawnProcess>; pidFile: string } {
	const dir = scratchDir(label);
	const pidFile = path.join(dir, 'survivor.pid');
	const engine = spawnProcess({
		name: 'engine',
		launcher: path.join(fakePayload(dir, mode), 'bin/opendj-engine'),
		dataDir: path.join(dir, 'data'),
		logPath: path.join(dir, '(private path)'),
		port: 0,
		env: { FAKE_ENGINE_CHILD_PID_FILE: pidFile, ...env }
	});
	return { engine, pidFile };
}

test('a worker left behind when the leader exits on its own is swept', { skip: POSIX_GROUPS }, async () => {
	const { engine, pidFile } = spawnGroupFixture('orphan-sweep', 'leader-exit-child-lives', { FAKE_ENGINE_LEADER_EXIT_MS: '1500' });
	const survivor = await survivorOf(pidFile);
	assert.equal(pidGone(survivor), false, 'the worker must be alive before the leader goes, or this proves nothing');
	assert.equal((await engine.waitReap()).code, 0);
	await waitFor(() => pidGone(survivor), 5_000);
	await engine.shutdown();
	assert.equal(pidGone(survivor), true);
});

test('a worker that ignores SIGTERM does not outlive a leader that honored it', { skip: POSIX_GROUPS }, async () => {
	const { engine, pidFile } = spawnGroupFixture('orphan-sweep-term', 'leader-term-child-ignores-term', {});
	const survivor = await survivorOf(pidFile);
	await engine.shutdown();
	await waitFor(() => pidGone(survivor), 5_000);
	assert.equal(pidGone(survivor), true);
});

test('stopping an adopted engine also sweeps what its leader leaves behind', { skip: POSIX_GROUPS }, async () => {
	const dir = scratchDir('orphan-sweep-adopted');
	const pidFile = path.join(dir, 'survivor.pid');
	const holder = spawn(process.execPath, [FAKE_ENGINE, '--data-dir', dir, '--host', '127.0.0.1', '--port', '0'], {
		detached: true,
		stdio: 'ignore',
		env: { ...process.env, FAKE_ENGINE_MODE: 'leader-term-child-ignores-term', FAKE_ENGINE_CHILD_PID_FILE: pidFile }
	});
	const survivor = await survivorOf(pidFile);
	await stopHolderPid(holder.pid ?? 0);
	await waitFor(() => pidGone(survivor), 5_000);
	assert.equal(pidGone(survivor), true);
});

test('the sweep makes no signal attempt on Windows, for a pgid of 0 or below, or for a pid in use again', () => {
	const sent: Array<[number, NodeJS.Signals]> = [];
	const send = (pid: number, signal: NodeJS.Signals): boolean => (sent.push([pid, signal]), true);
	sweepOrphanedGroup(4242, 'engine', 'win32', () => false, send);
	sweepOrphanedGroup(0, 'engine', 'linux', () => false, send);
	sweepOrphanedGroup(-1, 'engine', 'linux', () => false, send);
	sweepOrphanedGroup(4242, 'engine', 'linux', () => true, send);
	assert.deepEqual(sent, []);
});

test('the sweep SIGKILLs the group when the leader pid is free', () => {
	const sent: Array<[number, NodeJS.Signals]> = [];
	sweepOrphanedGroup(4242, 'engine', 'linux', () => false, (pid, signal) => (sent.push([pid, signal]), true));
	assert.deepEqual(sent, [[4242, 'SIGKILL']]);
});
