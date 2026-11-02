// The INSTALL-23 state machine, driven with real engine processes and a
// recording surface in place of the window.
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { ENGINE_LOG_MIN_FREE_BYTES } from '../src/engine-log';
import { freeLoopbackPort, spawnEngine } from '../src/engine';
import { makeLockProbe } from '../src/launch';
import { ShellHealthServer } from '../src/shell-health';
import { installShellLogging } from '../src/shell-log';
import {
	EngineSupervisor,
	PARENT_FILE,
	RECOVERY_TIMEOUT_MS,
	type SupervisorDeps,
	type SupervisorSurface,
	defaultDeps,
	exitReason,
	readParentFilePid,
	releaseParentWatchOwnership,
	shutdownSupervised,
	supervisedOrigin,
	writeParentFile
} from '../src/supervisor';
import { REPO_ROOT, fakePayload, pidGone, resolveRepoPython, scratchDir, waitFor } from './helpers';

// Supervisor decisions go to the shell log, as in the Tauri shell.
const shellLog = path.join(scratchDir('supervisor-shell-log'), 'shell.log');
installShellLogging(shellLog);

// ----- exit naming (#2801) ----------------------------------------------------
test('a signal kill is named by its signal', () => {
	assert.equal(exitReason({ exitCode: null, signal: 'SIGKILL' }), 'engine-exited(signal SIGKILL)');
});
test('an ordinary exit is named by its code', () => {
	assert.equal(exitReason({ exitCode: 1, signal: null }), 'engine-exited(code 1)');
});
test('a signal wins over a placeholder exit code', () => {
	assert.equal(exitReason({ exitCode: 1, signal: 'SIGKILL' }), 'engine-exited(signal SIGKILL)');
});
test('neither signal nor code says so rather than guessing', () => {
	assert.equal(exitReason({ exitCode: null, signal: null }), 'engine-exited(unknown)');
});

// ----- the machine ----------------------------------------------------------------
interface Recorded {
	titles: string[];
	origins: string[];
	fatal: { exitCode: number; pid: number; port: number; healthPort: number }[];
	dialogs: string[];
	quits: number[];
}

function recordingSurface(choice: 'relaunch' | 'quit' = 'quit'): { surface: SupervisorSurface; seen: Recorded } {
	const seen: Recorded = { titles: [], origins: [], fatal: [], dialogs: [], quits: [] };
	return {
		seen,
		surface: {
			setTitle: (title) => seen.titles.push(title),
			navigateToOrigin: (origin) => seen.origins.push(origin),
			navigateFatal: (info) => seen.fatal.push(info),
			showFatalDialog: async (detail) => {
				seen.dialogs.push(detail);
				return choice;
			},
			quit: (code) => seen.quits.push(code)
		}
	};
}

async function running(mode = 'healthy', overrides: Partial<SupervisorDeps> = {}, choice: 'relaunch' | 'quit' = 'quit') {
	const dir = scratchDir(`supervisor-${mode}`);
	const payload = fakePayload(dir, mode);
	const dataDir = path.join(dir, 'data');
	const logPath = path.join(dataDir, 'logs/engine.log');
	const engine = spawnEngine(fakePayload(dir, 'healthy'), dataDir, logPath, await freeLoopbackPort());
	await engine.waitUntilHealthy(10_000);
	const health = await ShellHealthServer.start(dataDir, () => '{}');
	const { surface, seen } = recordingSurface(choice);
	const deps: SupervisorDeps = { ...defaultDeps(makeLockProbe('python3')), bootTimeoutMs: 5_000, ...overrides };
	// The restart payload is `mode`; the first engine is always healthy.
	fs.rmSync(payload, { recursive: true });
	fakePayload(dir, mode);
	const supervisor = new EngineSupervisor(
		{ kind: 'spawned', engine },
		{ payload, dataDir, logPath, productName: 'Open DJ' },
		health,
		surface,
		deps
	);
	return { supervisor, engine, health, seen, logPath };
}

test('a healthy engine publishes running and the plain title', async () => {
	const { supervisor, engine, health, seen } = await running();
	try {
		await supervisor.tick();
		assert.equal(supervisor.phase, 'running');
		assert.equal(health.current().engine, 'running');
		assert.equal(health.current().lock_pid, engine.pid);
		assert.deepEqual(seen.titles, ['Open DJ']);
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('a killed engine is detected, reaped, and restarted on a fresh port', async () => {
	const { supervisor, engine, health, seen } = await running();
	try {
		process.kill(engine.pid, 'SIGKILL');
		await engine.waitReap();
		await supervisor.tick();
		assert.equal(supervisor.phase, 'restarting');
		assert.equal(seen.fatal.length, 1, 'the dead state is shown before the restart');
		assert.equal(seen.fatal[0]?.healthPort, health.port);
		await supervisor.tick();
		assert.equal(supervisor.phase, 'running');
		const current = supervisor.current();
		assert.ok(current !== null && current.kind === 'spawned');
		assert.notEqual(current.engine.pid, engine.pid);
		assert.deepEqual(seen.origins, [supervisedOrigin(current)]);
		const log = fs.readFileSync(shellLog, 'utf8');
		assert.match(log, /engine-exited\(signal SIGKILL\)/);
		assert.match(log, /\d{4}-\d\d-\d\dT[\d:.]+\+00:00 engine restarted after exit code -1/);
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('a restart that fails goes fatal and shows the relaunch page, not a spinner', async () => {
	const { supervisor, engine, health, seen } = await running('exit');
	try {
		process.kill(engine.pid, 'SIGKILL');
		await engine.waitReap();
		await supervisor.tick(); // dead -> restarting
		await supervisor.tick(); // restart attempt fails
		assert.equal(supervisor.phase, 'fatal');
		assert.equal(health.current().engine, 'dead');
		assert.equal(seen.fatal.length, 2);
		assert.equal(seen.titles.at(-1), 'Open DJ - engine dead');
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('a failed restart on a full disk waits for space instead of going fatal', async () => {
	const { supervisor, engine, health, seen } = await running('exit', { diskFree: () => 0 });
	try {
		process.kill(engine.pid, 'SIGKILL');
		await engine.waitReap();
		await supervisor.tick();
		await supervisor.tick();
		assert.equal(supervisor.phase, 'awaiting-disk-space');
		assert.equal(health.current().reason, 'low-disk');
		assert.equal(seen.titles.at(-1), 'Open DJ - waiting for disk space');
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('once disk space is back, the waiting engine is restarted', async () => {
	let free = 0;
	const { supervisor, engine, health } = await running('healthy', { diskFree: () => free });
	try {
		supervisor.phase = 'awaiting-disk-space';
		await supervisor.tick();
		assert.equal(supervisor.phase, 'awaiting-disk-space', 'no restart while the disk is still full');
		free = 2 * ENGINE_LOG_MIN_FREE_BYTES;
		process.kill(engine.pid, 'SIGKILL');
		await engine.waitReap();
		await supervisor.tick();
		assert.equal(supervisor.phase, 'running');
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('still restarting after the recovery timeout raises the native fatal dialog', async () => {
	let now = 0;
	const dir = scratchDir('supervisor-timeout');
	const dataDir = path.join(dir, 'data');
	const logPath = path.join(dataDir, 'logs/engine.log');
	const engine = spawnEngine(fakePayload(dir), dataDir, logPath, await freeLoopbackPort());
	await engine.waitUntilHealthy(10_000);
	const health = await ShellHealthServer.start(dataDir, () => '{}');
	const { surface, seen } = recordingSurface('quit');
	const supervisor = new EngineSupervisor(
		{ kind: 'spawned', engine },
		{ payload: path.join(dir, 'payload'), dataDir, logPath, productName: 'Open DJ' },
		health,
		surface,
		defaultDeps(makeLockProbe('python3')),
		() => now
	);
	try {
		process.kill(engine.pid, 'SIGKILL');
		await engine.waitReap();
		await supervisor.tick(); // -> restarting, dead_at = 0
		supervisor.phase = 'restarting';
		(supervisor as unknown as { autoRestartAttempted: boolean }).autoRestartAttempted = true;
		now = RECOVERY_TIMEOUT_MS - 1;
		await supervisor.tick();
		assert.equal(supervisor.phase, 'restarting', 'not fatal one millisecond early');
		now = RECOVERY_TIMEOUT_MS;
		await supervisor.tick();
		assert.equal(supervisor.phase, 'fatal');
		assert.ok(await waitFor(() => seen.quits.length === 1));
		assert.match(seen.dialogs[0] as string, /The engine exited with code -1/);
		assert.deepEqual(seen.quits, [1]);
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('shutdown stops the supervised engine and its group', async () => {
	const { supervisor, engine, health } = await running();
	await supervisor.shutdown();
	await health.close();
	assert.ok(await waitFor(() => pidGone(engine.pid)));
	assert.equal(supervisor.current(), null);
});

// ----- liveness and quit during restart (EL-17-F10) ------------------------------
// fakePayload writes a POSIX `#!/bin/sh` launcher, so the tests that run it are POSIX-only.
const POSIX_LAUNCHER = process.platform === 'win32' && 'fakePayload launcher is a POSIX shell script';

// Pinned to the Tauri shell's UNRESPONSIVE_AFTER_MISSES. A literal, so that moving the constant cannot move the test with it.
test('a live engine that misses fewer than six health probes stays running', { skip: POSIX_LAUNCHER }, async () => {
	const { supervisor, health } = await running('healthy', { health: async () => false });
	try {
		for (let miss = 1; miss < 6; miss += 1) {
			await supervisor.tick();
			assert.equal(supervisor.phase, 'running', `still running after ${miss} missed probe(s)`);
		}
		await supervisor.tick();
		assert.equal(supervisor.phase, 'restarting');
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('quitting after a death stops the restart before it spawns an engine', { skip: POSIX_LAUNCHER }, async () => {
	let spawns = 0;
	const { supervisor, engine, health } = await running('healthy', {
		spawn: (payload, dataDir, logPath, port) => {
			spawns += 1;
			return spawnEngine(payload, dataDir, logPath, port);
		}
	});
	await engine.shutdown();
	await supervisor.tick();
	assert.equal(supervisor.phase, 'restarting');
	await supervisor.shutdown();
	await supervisor.tick();
	assert.equal(spawns, 0);
	assert.notEqual(supervisor.phase, 'running');
	await health.close();
});

test('quitting from inside a restart, after its port is picked, spawns no engine', { skip: POSIX_LAUNCHER }, async () => {
	let spawns = 0;
	let current: EngineSupervisor | null = null;
	const { supervisor, engine, health } = await running('healthy', {
		freePort: async () => {
			// Not awaited: shutdown waits for the restart this call is part of.
			void current?.shutdown();
			return freeLoopbackPort();
		},
		spawn: (payload, dataDir, logPath, port) => {
			spawns += 1;
			return spawnEngine(payload, dataDir, logPath, port);
		}
	});
	current = supervisor;
	try {
		process.kill(engine.pid, 'SIGKILL');
		await engine.waitReap();
		await supervisor.tick();
		await supervisor.tick();
		assert.equal(spawns, 0);
		assert.notEqual(supervisor.phase, 'running');
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('quitting while a restart waits for health cancels the wait, not the boot timeout', { skip: POSIX_LAUNCHER }, async () => {
	const spawned: ReturnType<typeof spawnEngine>[] = [];
	const { supervisor, health } = await running('never-ready', {
		bootTimeoutMs: 60_000,
		spawn: (payload, dataDir, logPath, port) => {
			const engine = spawnEngine(payload, dataDir, logPath, port);
			spawned.push(engine);
			return engine;
		}
	});
	try {
		const restart = supervisor.requestRelaunch();
		assert.ok(await waitFor(() => spawned.length === 1), 'the restart spawned its engine');
		const quitStarted = Date.now();
		await supervisor.shutdown();
		await restart;
		assert.ok(Date.now() - quitStarted < 10_000, 'quit returned well before the 60 s boot timeout');
		assert.ok(await waitFor(() => pidGone(spawned[0]!.pid)), 'the engine the restart spawned is gone');
	} finally {
		await supervisor.shutdown();
		await health.close();
	}
});

test('shutdown leaves an adopted engine running', async () => {
	const pid = process.pid;
	await shutdownSupervised({ kind: 'adopted', pid, host: '127.0.0.1', port: 9 });
	assert.ok(!pidGone(pid), 'adopted shutdown must not stop the holder pid');
});

test('adopt quit clears .engine.parent when it names this shell so parent_watch survives', async () => {
	const { spawn } = await import('node:child_process');
	const python = resolveRepoPython();
	const spawner = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 120_000)'], {
		detached: true,
		stdio: 'ignore'
	});
	const dir = scratchDir('parent-watch-rebind');
	const dataDir = path.join(dir, 'data');
	fs.mkdirSync(dataDir, { recursive: true });
	const spawnerPid = spawner.pid;
	assert.ok(spawnerPid !== undefined);
	spawner.unref();
	const child = spawn(
		python,
		[
			'-c',
			`import sys, time
from pathlib import Path
from apps.engine_core import parent_watch
parent_watch.start(Path(sys.argv[1]))
time.sleep(600)`,
			dataDir
		],
		{
			cwd: REPO_ROOT,
			env: { ...process.env, OPENDJ_PARENT_PID: String(spawnerPid) },
			stdio: 'ignore',
			detached: true
		}
	);
	const childPid = child.pid;
	assert.ok(childPid !== undefined);
	child.unref();
	try {
		fs.writeFileSync(path.join(dataDir, PARENT_FILE), `${process.pid}\n`);
		await shutdownSupervised({ kind: 'adopted', pid: 1, host: '127.0.0.1', port: 9 }, dataDir);
		assert.equal(fs.existsSync(path.join(dataDir, PARENT_FILE)), false);
		assert.ok(!pidGone(childPid), 'engine child survives after adopt shell rebinds parent watch');
		spawner.kill('SIGKILL');
		assert.ok(await waitFor(() => pidGone(childPid), 3_000), 'engine child exits when spawner parent dies');
	} finally {
		if (!pidGone(childPid)) process.kill(childPid, 'SIGKILL');
		if (!pidGone(spawnerPid)) spawner.kill('SIGKILL');
	}
});

test('quit during restart adopt abort clears .engine.parent after writeParentFile', () => {
	const dir = scratchDir('parent-watch-restart-adopt-abort');
	const dataDir = path.join(dir, 'data');
	fs.mkdirSync(dataDir, { recursive: true });
	writeParentFile(dataDir);
	releaseParentWatchOwnership(dataDir);
	assert.equal(fs.existsSync(path.join(dataDir, PARENT_FILE)), false);
});

test('releaseParentWatchOwnership is a no-op when the file names another shell', () => {
	const dir = scratchDir('parent-watch-noop');
	const dataDir = path.join(dir, 'data');
	fs.mkdirSync(dataDir, { recursive: true });
	const file = path.join(dataDir, PARENT_FILE);
	fs.writeFileSync(file, '424242\n');
	releaseParentWatchOwnership(dataDir);
	assert.equal(fs.readFileSync(file, 'utf8'), '424242\n');
});

test('adopt quit restores an earlier live adopter instead of falling back to a dead spawner', async () => {
	const { spawn } = await import('node:child_process');
	const python = resolveRepoPython();
	const spawner = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 120_000)'], {
		detached: true,
		stdio: 'ignore'
	});
	const dir = scratchDir('parent-watch-chain');
	const dataDir = path.join(dir, 'data');
	fs.mkdirSync(dataDir, { recursive: true });
	const spawnerPid = spawner.pid;
	assert.ok(spawnerPid !== undefined);
	spawner.unref();
	const earlierAdopter = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 120_000)'], {
		detached: true,
		stdio: 'ignore'
	});
	const earlierPid = earlierAdopter.pid;
	assert.ok(earlierPid !== undefined);
	earlierAdopter.unref();
	const child = spawn(
		python,
		[
			'-c',
			`import sys, time
from pathlib import Path
from apps.engine_core import parent_watch
parent_watch.start(Path(sys.argv[1]))
time.sleep(600)`,
			dataDir
		],
		{
			cwd: REPO_ROOT,
			env: { ...process.env, OPENDJ_PARENT_PID: String(spawnerPid) },
			stdio: 'ignore',
			detached: true
		}
	);
	const childPid = child.pid;
	assert.ok(childPid !== undefined);
	child.unref();
	try {
		fs.writeFileSync(path.join(dataDir, PARENT_FILE), `${earlierPid}\n`);
		writeParentFile(dataDir);
		assert.equal(readParentFilePid(dataDir), process.pid);
		await shutdownSupervised({ kind: 'adopted', pid: 1, host: '127.0.0.1', port: 9 }, dataDir);
		assert.equal(readParentFilePid(dataDir), earlierPid);
		assert.ok(!pidGone(childPid), 'engine child survives when parent_watch rebinds to earlier adopter');
		spawner.kill('SIGKILL');
		earlierAdopter.kill('SIGKILL');
		assert.ok(await waitFor(() => pidGone(childPid), 3_000), 'engine child exits when last adopter dies');
	} finally {
		if (!pidGone(childPid)) process.kill(childPid, 'SIGKILL');
		if (!pidGone(earlierPid)) earlierAdopter.kill('SIGKILL');
		if (!pidGone(spawnerPid)) spawner.kill('SIGKILL');
	}
});

test('restart release stops a hung adopted holder but quit shutdown does not', async () => {
	const { spawn } = await import('node:child_process');
	const { releaseSupervisedForRestart, shutdownSupervised } = await import('../src/supervisor');
	const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 120_000)'], {
		detached: true,
		stdio: 'ignore'
	});
	const pid = child.pid;
	assert.ok(pid !== undefined && pid > 0);
	child.unref();
	try {
		const adopted = { kind: 'adopted' as const, pid, host: '127.0.0.1', port: 9 };
		await shutdownSupervised(adopted);
		assert.ok(!pidGone(pid), 'quit path must not stop adopted holder');
		await releaseSupervisedForRestart(adopted);
		assert.ok(await waitFor(() => pidGone(pid)), 'restart path must clear a hung adopted holder');
	} finally {
		if (!pidGone(pid)) process.kill(pid, 'SIGKILL');
	}
});
