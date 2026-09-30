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
	RECOVERY_TIMEOUT_MS,
	type SupervisorDeps,
	type SupervisorSurface,
	defaultDeps,
	exitReason,
	supervisedOrigin
} from '../src/supervisor';
import { fakePayload, pidGone, scratchDir, waitFor } from './helpers';

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
