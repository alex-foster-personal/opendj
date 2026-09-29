// Post-boot engine supervision: reap, restart, fatal UI, shell health.
//
// Port of apps/desktop/src-tauri/src/supervisor.rs (INSTALL-23). The state
// machine, poll interval, recovery timeout, log lines and window titles are
// the same. The window and dialogs are reached through `SupervisorSurface`, so
// the machine is testable without an Electron runtime.

import * as fs from 'node:fs';
import * as path from 'node:path';

import { ENGINE_LOG_MIN_FREE_BYTES, diskFreeBytes } from './engine-log';
import {
	BOOT_TIMEOUT_MS,
	type Engine,
	type ExitInfo,
	freeLoopbackPort,
	healthOk,
	spawnEngine
} from './engine';
import {
	type LaunchPlan,
	type LockProbe,
	inspectLock,
	lockPath,
	originForAdopt,
	pidCanAct,
	readLockFields,
	stopHolderPid
} from './launch';
import type { ShellHealthServer } from './shell-health';
import { appendShellLog, utcTimestampIso } from './shell-log';

export const POLL_INTERVAL_MS = 5_000;
export const RECOVERY_TIMEOUT_MS = 30_000;
export const PARENT_FILE = '.engine.parent';

export type SupervisorPhase =
	| 'running'
	| 'dead'
	| 'reaping'
	| 'restarting'
	| 'awaiting-disk-space'
	| 'fatal'
	| 'awaiting-relaunch';

export type Supervised =
	| { kind: 'spawned'; engine: Engine }
	| { kind: 'adopted'; pid: number; host: string; port: number };

export function supervisedOrigin(s: Supervised): string {
	return s.kind === 'spawned' ? s.engine.origin() : originForAdopt(s.host, s.port);
}

export function supervisedPort(s: Supervised): number {
	return s.kind === 'spawned' ? s.engine.port : s.port;
}

export function supervisedPid(s: Supervised): number {
	return s.kind === 'spawned' ? s.engine.pid : s.pid;
}

export async function shutdownSupervised(s: Supervised): Promise<void> {
	if (s.kind === 'spawned') await s.engine.shutdown();
	else await stopHolderPid(s.pid);
}

export interface SupervisorPaths {
	payload: string;
	dataDir: string;
	logPath: string;
	productName: string;
}

/** Everything the machine does to the outside world. */
export interface SupervisorSurface {
	setTitle(title: string): void;
	navigateToOrigin(origin: string): void;
	navigateFatal(info: { exitCode: number; pid: number; port: number; healthPort: number }): void;
	/** Native Relaunch/Quit dialog. Resolves with the user's choice. */
	showFatalDialog(detail: string): Promise<'relaunch' | 'quit'>;
	quit(code: number): void;
}

/** Seams for tests; production uses the real engine, lock and disk. */
export interface SupervisorDeps {
	health: (port: number) => Promise<boolean>;
	lockProbe: (lock: string) => LockProbe | null;
	spawn: (payload: string, dataDir: string, logPath: string, port: number) => Engine;
	freePort: () => Promise<number>;
	diskFree: (dir: string) => number;
	pidCanAct: (pid: number) => boolean;
	bootTimeoutMs: number;
}

export function defaultDeps(lockProbe: (lock: string) => LockProbe | null): SupervisorDeps {
	return {
		health: (port) => healthOk(port),
		lockProbe,
		spawn: spawnEngine,
		freePort: freeLoopbackPort,
		diskFree: diskFreeBytes,
		pidCanAct,
		bootTimeoutMs: BOOT_TIMEOUT_MS
	};
}

export interface DeathInfo {
	exitCode: number | null;
	/** Only known when this shell is the real parent (spawned). */
	signal: string | null;
	lockPid: number | null;
	lockPort: number | null;
}

/** `engine-exited(signal N)` / `engine-exited(code N)`; a signal wins over a stray code. */
export function exitReason(death: Pick<DeathInfo, 'exitCode' | 'signal'>): string {
	if (death.signal !== null) return `engine-exited(signal ${death.signal})`;
	if (death.exitCode !== null) return `engine-exited(code ${death.exitCode})`;
	return 'engine-exited(unknown)';
}

export function writeParentFile(dataDir: string): void {
	const file = path.join(dataDir, PARENT_FILE);
	fs.writeFileSync(file, `${process.pid}\n`, { mode: 0o600 });
	fs.chmodSync(file, 0o600);
}

export class EngineSupervisor {
	phase: SupervisorPhase = 'running';
	private supervised: Supervised | null;
	private deadAt: number | null = null;
	private exitCode: number | null = null;
	private lockPid: number | null = null;
	private lockPort: number | null = null;
	private autoRestartAttempted = false;
	private stopped = false;
	private busy: Promise<void> = Promise.resolve();

	constructor(
		supervised: Supervised,
		readonly paths: SupervisorPaths,
		readonly shellHealth: ShellHealthServer,
		private readonly surface: SupervisorSurface,
		private readonly deps: SupervisorDeps,
		private readonly now: () => number = Date.now
	) {
		this.supervised = supervised;
	}

	current(): Supervised | null {
		return this.supervised;
	}

	/** Run the poll loop until `shutdown`. */
	start(pollMs = POLL_INTERVAL_MS): void {
		const loop = async (): Promise<void> => {
			while (!this.stopped) {
				await this.serialized(() => this.tick());
				if (this.shellHealth.takeRelaunchRequest()) await this.requestRelaunch();
				await new Promise((resolve) => setTimeout(resolve, pollMs));
			}
		};
		void loop();
	}

	/** Ticks and relaunches never overlap: each waits for the previous one. */
	private serialized(work: () => Promise<void>): Promise<void> {
		const next = this.busy.then(work, work);
		this.busy = next.catch((error: unknown) => appendShellLog('ERROR', `supervisor step failed: ${String(error)}`));
		return this.busy;
	}

	async shutdown(): Promise<void> {
		this.stopped = true;
		await this.busy;
		const running = this.supervised;
		this.supervised = null;
		if (running !== null) await shutdownSupervised(running);
	}

	requestRelaunch(): Promise<void> {
		appendShellLog('INFO', 'user chose relaunch');
		return this.serialized(async () => {
			await this.attemptRestart();
		});
	}

	async tick(): Promise<void> {
		switch (this.phase) {
			case 'running': {
				const dead = await this.detectDeath();
				if (dead === null) {
					this.publishRunning();
					return;
				}
				this.phase = 'dead';
				this.deadAt = this.now();
				this.exitCode = dead.exitCode;
				this.lockPid = dead.lockPid;
				this.lockPort = dead.lockPort;
				appendShellLog('WARN', `${exitReason(dead)} pid=${dead.lockPid ?? 0} port=${dead.lockPort ?? 0}`);
				this.updateSurfaces();
				await this.reapChild();
				this.phase = 'restarting';
				this.autoRestartAttempted = false;
				this.updateSurfaces();
				return;
			}
			case 'restarting': {
				const deadAt = this.deadAt ?? this.now();
				if (!this.autoRestartAttempted) {
					this.autoRestartAttempted = true;
					if (await this.attemptRestart()) return;
					if (this.isLowDisk()) {
						this.enterAwaitingDisk();
						return;
					}
				}
				if (this.now() - deadAt >= RECOVERY_TIMEOUT_MS && this.phase === 'restarting') {
					if (this.isLowDisk()) {
						this.enterAwaitingDisk();
					} else {
						this.phase = 'fatal';
						appendShellLog('ERROR', 'engine fatal: restart failed within 30s');
						this.updateSurfaces();
						void this.showFatalDialog();
					}
				} else if (this.phase === 'restarting') {
					this.publishRestarting();
				}
				return;
			}
			case 'awaiting-disk-space': {
				if (this.diskRecovered()) {
					if (await this.attemptRestart()) {
						appendShellLog('INFO', 'engine restarted after low disk recovered');
						return;
					}
					this.phase = 'awaiting-disk-space';
				}
				this.publishAwaitingDisk();
				return;
			}
			case 'fatal':
			case 'awaiting-relaunch':
			case 'dead':
				this.publishDead();
				return;
			case 'reaping':
				return;
		}
	}

	private async detectDeath(): Promise<DeathInfo | null> {
		const holder = readLockFields(lockPath(this.paths.dataDir));
		const running = this.supervised;
		if (running === null) return null;
		if (running.kind === 'spawned') {
			const engine = running.engine;
			const exit: ExitInfo | null = engine.tryReap();
			const lockPid = holder?.pid ?? engine.pid;
			const lockPort = holder?.port ?? engine.port;
			if (exit !== null) return { exitCode: exit.code, signal: exit.signal, lockPid, lockPort };
			if (engine.isLogFailed()) return { exitCode: 1, signal: null, lockPid, lockPort };
			if (!(await this.deps.health(engine.port))) {
				if (!this.deps.pidCanAct(engine.pid)) {
					return { exitCode: 1, signal: null, lockPid: engine.pid, lockPort: engine.port };
				}
				return { exitCode: 1, signal: null, lockPid, lockPort: engine.port };
			}
			return null;
		}
		if (!this.deps.pidCanAct(running.pid) || !(await this.deps.health(running.port))) {
			return { exitCode: 1, signal: null, lockPid: running.pid, lockPort: running.port };
		}
		return null;
	}

	private async reapChild(): Promise<void> {
		this.phase = 'reaping';
		const running = this.supervised;
		if (running?.kind === 'spawned' && running.engine.tryReap() === null) {
			// Detected by health, not by exit: make sure it is really gone.
			await running.engine.shutdown();
		}
		appendShellLog('INFO', 'engine child reaped');
	}

	private async attemptRestart(): Promise<boolean> {
		const exitCode = this.exitCode ?? -1;
		const { payload, dataDir, logPath } = this.paths;
		const old = this.supervised;
		this.supervised = null;
		if (old !== null) await shutdownSupervised(old);
		try {
			writeParentFile(dataDir);
		} catch (error) {
			return this.restartFailed(`restart failed: ${(error as Error).message}`);
		}
		const plan: LaunchPlan = await inspectLock(lockPath(dataDir), this.deps.health, this.deps.lockProbe);
		if (plan.kind === 'adopt') {
			this.supervised = { kind: 'adopted', pid: plan.pid, host: plan.host, port: plan.port };
			return this.restarted(exitCode);
		}
		if (plan.kind === 'stop-or-quit') {
			return this.restartFailed(`engine fatal: lock held by pid ${plan.pid}: ${plan.detail}`);
		}
		let engine: Engine;
		try {
			const port = await this.deps.freePort();
			engine = this.deps.spawn(payload, dataDir, logPath, port);
		} catch (error) {
			return this.restartFailed(`restart failed: ${(error as Error).message}`);
		}
		try {
			await engine.waitUntilHealthy(this.deps.bootTimeoutMs);
		} catch (error) {
			await engine.shutdown();
			return this.restartFailed(`restart failed: ${(error as Error).message}`);
		}
		this.supervised = { kind: 'spawned', engine };
		return this.restarted(exitCode);
	}

	private restarted(exitCode: number): boolean {
		this.phase = 'running';
		this.deadAt = null;
		this.autoRestartAttempted = false;
		appendShellLog('INFO', `${utcTimestampIso()} engine restarted after exit code ${exitCode}`);
		this.updateSurfaces();
		const running = this.supervised;
		if (running !== null) this.surface.navigateToOrigin(supervisedOrigin(running));
		this.surface.setTitle(this.paths.productName);
		return true;
	}

	private restartFailed(line: string): boolean {
		appendShellLog('ERROR', line);
		this.phase = 'fatal';
		this.updateSurfaces();
		return false;
	}

	private isLowDisk(): boolean {
		try {
			return this.deps.diskFree(this.paths.dataDir) < ENGINE_LOG_MIN_FREE_BYTES;
		} catch {
			return false;
		}
	}

	private diskRecovered(): boolean {
		try {
			return this.deps.diskFree(this.paths.dataDir) >= ENGINE_LOG_MIN_FREE_BYTES;
		} catch {
			return false;
		}
	}

	private enterAwaitingDisk(): void {
		this.phase = 'awaiting-disk-space';
		appendShellLog('WARN', 'engine awaiting disk space recovery');
		this.updateSurfaces();
	}

	private publishAwaitingDisk(): void {
		this.shellHealth.update({
			status: 'waiting',
			engine: 'waiting-disk',
			lock_pid: this.lockPid,
			lock_port: this.lockPort,
			exit_code: this.exitCode,
			reason: 'low-disk'
		});
		this.surface.setTitle(`${this.paths.productName} - waiting for disk space`);
	}

	private publishRunning(): void {
		const running = this.supervised;
		const fields =
			readLockFields(lockPath(this.paths.dataDir)) ??
			(running === null ? null : { pid: supervisedPid(running), port: supervisedPort(running) });
		this.shellHealth.update({
			status: 'ok',
			engine: 'running',
			lock_pid: fields?.pid ?? 0,
			lock_port: fields?.port ?? 0,
			exit_code: null,
			reason: null
		});
		this.surface.setTitle(this.paths.productName);
	}

	private publishRestarting(): void {
		this.shellHealth.update({
			status: 'restarting',
			engine: 'restarting',
			lock_pid: this.lockPid,
			lock_port: this.lockPort,
			exit_code: this.exitCode,
			reason: null
		});
		this.surface.setTitle(`${this.paths.productName} - engine restarting`);
	}

	private publishDead(): void {
		this.shellHealth.update({
			status: 'dead',
			engine: 'dead',
			lock_pid: this.lockPid,
			lock_port: this.lockPort,
			exit_code: this.exitCode,
			reason: null
		});
		this.surface.setTitle(`${this.paths.productName} - engine dead`);
	}

	private updateSurfaces(): void {
		switch (this.phase) {
			case 'running':
				this.publishRunning();
				return;
			case 'restarting':
				this.publishRestarting();
				return;
			case 'awaiting-disk-space':
				this.publishAwaitingDisk();
				return;
			case 'dead':
			case 'fatal':
			case 'awaiting-relaunch':
				this.publishDead();
				this.surface.navigateFatal({
					exitCode: this.exitCode ?? -1,
					pid: this.lockPid ?? 0,
					port: this.lockPort ?? 0,
					healthPort: this.shellHealth.port
				});
				return;
			case 'reaping':
				return;
		}
	}

	private async showFatalDialog(): Promise<void> {
		const detail =
			`The engine exited with code ${this.exitCode ?? -1} (pid ${this.lockPid ?? 0}, port ${this.lockPort ?? 0}).\n\n` +
			'Relaunch starts a fresh engine, or quit the app.';
		const choice = await this.surface.showFatalDialog(detail);
		if (choice === 'relaunch') {
			appendShellLog('INFO', 'user chose relaunch from native dialog');
			await this.requestRelaunch();
		} else {
			appendShellLog('INFO', 'engine fatal: restart declined');
			this.surface.quit(1);
		}
	}
}
