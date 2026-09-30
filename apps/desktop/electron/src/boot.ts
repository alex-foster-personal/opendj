// Start or adopt the engine and wait for a healthy origin.
//
// Port of `start_engine` / `spawn_fresh_engine` in src-tauri/src/main.rs.
// Runs before the window exists, on purpose: the failure path must raise a
// native dialog rather than open a blank window.

import { BOOT_TIMEOUT_MS, type Engine, EngineError, freeLoopbackPort, healthOk, spawnEngine } from './engine';
import { type LaunchPlan, type LockProbe, inspectLock, lockPath, stopHolderPid } from './launch';
import { logTail } from './shell-log';
import { type Supervised, writeParentFile } from './supervisor';

export interface BootDeps {
	health: (port: number) => Promise<boolean>;
	lockProbe: (lock: string) => LockProbe | null;
	spawn: (payload: string, dataDir: string, logPath: string, port: number) => Engine;
	freePort: () => Promise<number>;
	stopHolder: (pid: number) => Promise<void>;
	/** Native "Stop engine" / "Quit" dialog. True means stop it and start fresh. */
	askStopOrQuit: (pid: number, detail: string, lock: string) => Promise<boolean>;
	bootTimeoutMs: number;
}

export function defaultBootDeps(
	lockProbe: (lock: string) => LockProbe | null,
	askStopOrQuit: BootDeps['askStopOrQuit']
): BootDeps {
	return {
		health: (port) => healthOk(port),
		lockProbe,
		spawn: spawnEngine,
		freePort: freeLoopbackPort,
		stopHolder: stopHolderPid,
		askStopOrQuit,
		bootTimeoutMs: BOOT_TIMEOUT_MS
	};
}

function parentFile(dataDir: string): void {
	try {
		writeParentFile(dataDir);
	} catch (error) {
		throw new EngineError('Open DJ could not record its shell parent.', String((error as Error).message));
	}
}

async function adopt(plan: Extract<LaunchPlan, { kind: 'adopt' }>, dataDir: string, deps: BootDeps, why: string): Promise<Supervised> {
	parentFile(dataDir);
	if (!(await deps.health(plan.port))) {
		throw new EngineError(
			'Open DJ could not adopt the running engine.',
			`pid ${plan.pid} ${why} at port ${plan.port}`
		);
	}
	return { kind: 'adopted', pid: plan.pid, host: plan.host, port: plan.port };
}

export async function startEngine(payload: string, dataDir: string, logPath: string, deps: BootDeps): Promise<Supervised> {
	const lock = lockPath(dataDir);
	const plan = await inspectLock(lock, deps.health, deps.lockProbe);
	if (plan.kind === 'adopt') return adopt(plan, dataDir, deps, 'holds the lock but is not answering health');
	if (plan.kind === 'stop-or-quit') {
		if (await deps.askStopOrQuit(plan.pid, plan.detail, lock)) {
			await deps.stopHolder(plan.pid);
			return spawnFreshEngine(payload, dataDir, logPath, deps);
		}
		throw new EngineError('Open DJ could not start.', plan.detail);
	}
	return spawnFreshEngine(payload, dataDir, logPath, deps);
}

export async function spawnFreshEngine(payload: string, dataDir: string, logPath: string, deps: BootDeps): Promise<Supervised> {
	parentFile(dataDir);
	const port = await deps.freePort();
	const running = deps.spawn(payload, dataDir, logPath, port);
	try {
		await running.waitUntilHealthy(deps.bootTimeoutMs);
		return { kind: 'spawned', engine: running };
	} catch (failure) {
		// Lost a race to another engine? Look at the lock again before failing.
		const lock = lockPath(dataDir);
		const plan = await inspectLock(lock, deps.health, deps.lockProbe);
		if (plan.kind === 'adopt') {
			await running.shutdown();
			return adopt(plan, dataDir, deps, 'answered the lock file but stopped answering health');
		}
		if (plan.kind === 'stop-or-quit') {
			await running.shutdown();
			if (await deps.askStopOrQuit(plan.pid, plan.detail, lock)) {
				await deps.stopHolder(plan.pid);
				return spawnFreshEngine(payload, dataDir, logPath, deps);
			}
			throw new EngineError('Open DJ could not start.', plan.detail);
		}
		await running.shutdown();
		if (failure instanceof EngineError) {
			const tail = logTail(logPath, 20);
			throw new EngineError(failure.headline, tail === '' ? failure.detail : `${failure.detail}\n\nLast engine output:\n${tail}`);
		}
		throw failure;
	}
}
