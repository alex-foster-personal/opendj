// Inspect `.engine.lock` before spawn and decide adopt vs spawn vs dialog.
//
// Port of apps/desktop/src-tauri/src/launch.rs. One difference, and why:
// Node has no flock(2), and the engine's lock IS a flock
// (apps/engine_core/lock.py). So "is it held?" is asked of the payload's own
// Python, which makes the same syscall the engine does. When that interpreter
// cannot be run, the holder pid's liveness answers instead: the kernel drops a
// flock when its holder exits and the engine opens the lock non-inheritable,
// so a live holder pid and a held lock are the same fact.

import { execFileSync, spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

import { sleep } from './engine';
import { appendShellLog } from './shell-log';

const LOCK_FILE_NAME = '.engine.lock';

export type LaunchPlan =
	| { kind: 'spawn' }
	| { kind: 'adopt'; pid: number; host: string; port: number }
	| { kind: 'stop-or-quit'; pid: number; detail: string };

interface LockJson {
	pid: number | null;
	host: string | null;
	port: number | null;
}

export function lockPath(dataDir: string): string {
	return path.join(dataDir, LOCK_FILE_NAME);
}

/** kill(pid, 0) == 0, exactly as the Rust shell asked it (EPERM counts as not ours). */
export function pidAlive(pid: number): boolean {
	if (!Number.isInteger(pid) || pid <= 0) return false;
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

function processState(pid: number): string | null {
	try {
		if (process.platform === 'linux') {
			const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
			const afterParen = stat.slice(stat.lastIndexOf(')') + 1).trim();
			return afterParen.charAt(0) || null;
		}
		const out = execFileSync('ps', ['-o', 'state=', '-p', String(pid)], {
			encoding: 'utf8',
			stdio: ['ignore', 'pipe', 'ignore']
		});
		return out.trim().charAt(0) || null;
	} catch {
		return null;
	}
}

/**
 * Whether a pid can still act (not dead, not a zombie). Unreadable process
 * state is treated as running, so callers never signal or respawn against
 * uncertainty (mirrors scripts/lib/owner_still_running.sh).
 */
export function pidCanAct(pid: number): boolean {
	if (pid === 0 || !pidAlive(pid)) return false;
	const state = processState(pid);
	return !(state === 'Z' || state === 'z');
}

export function parseLockJson(raw: string): LockJson {
	const empty: LockJson = { pid: null, host: null, port: null };
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return empty;
	}
	if (value === null || typeof value !== 'object' || Array.isArray(value)) return empty;
	const obj = value as Record<string, unknown>;
	const uint = (field: unknown): number | null =>
		typeof field === 'number' && Number.isInteger(field) && field >= 0 ? field : null;
	return {
		pid: uint(obj.pid),
		host: typeof obj.host === 'string' ? obj.host : null,
		port: uint(obj.port)
	};
}

/** The holder's pid and port, or null when the lock does not name both. */
export function readLockFields(lock: string): { pid: number; port: number } | null {
	let raw: string;
	try {
		raw = fs.readFileSync(lock, 'utf8');
	} catch {
		return null;
	}
	const holder = parseLockJson(raw);
	if (!holder.pid || !holder.port) return null;
	return { pid: holder.pid, port: holder.port };
}

/** What a lock probe reports: whether the flock is held, and the holder's JSON. */
export interface LockProbe {
	held: boolean;
	contents: string;
	/** How "held" was established, for the log and the dialog. */
	method: 'flock' | 'pid-liveness';
}

// The same call apps/engine_core/lock.py makes, from the other side.
const FLOCK_PROBE = [
	'import fcntl, json, os, sys',
	'fd = os.open(sys.argv[1], os.O_RDWR)',
	'try:',
	'    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)',
	'    held = False',
	'    fcntl.flock(fd, fcntl.LOCK_UN)',
	'except OSError:',
	'    held = True',
	'os.lseek(fd, 0, 0)',
	'contents = os.read(fd, 4096).decode("utf-8", "replace")',
	'print(json.dumps({"held": held, "contents": contents}))'
].join('\n');

/** Probe the flock with `python` (the payload interpreter). Null if it cannot run. */
export function flockProbe(python: string, lock: string): { held: boolean; contents: string } | null {
	const result = spawnSync(python, ['-I', '-c', FLOCK_PROBE, lock], {
		encoding: 'utf8',
		timeout: 5_000,
		env: { PATH: process.env.PATH ?? '' }
	});
	if (result.status !== 0 || typeof result.stdout !== 'string') return null;
	try {
		const parsed = JSON.parse(result.stdout) as { held: unknown; contents: unknown };
		if (typeof parsed.held !== 'boolean' || typeof parsed.contents !== 'string') return null;
		return { held: parsed.held, contents: parsed.contents };
	} catch {
		return null;
	}
}

/** Default probe: flock through the payload interpreter, pid liveness as the fallback. */
export function makeLockProbe(python: string | null): (lock: string) => LockProbe | null {
	return (lock) => {
		if (python !== null) {
			const viaFlock = flockProbe(python, lock);
			if (viaFlock !== null) return { ...viaFlock, method: 'flock' };
		}
		let contents: string;
		try {
			contents = fs.readFileSync(lock, 'utf8');
		} catch {
			return null;
		}
		const pid = parseLockJson(contents).pid ?? 0;
		return { held: pid > 0 && pidCanAct(pid), contents, method: 'pid-liveness' };
	};
}

export async function inspectLock(
	lock: string,
	healthCheck: (port: number) => Promise<boolean>,
	probe: (lock: string) => LockProbe | null
): Promise<LaunchPlan> {
	let isFile = false;
	try {
		isFile = fs.statSync(lock).isFile();
	} catch {
		isFile = false;
	}
	if (!isFile) return { kind: 'spawn' };
	const state = probe(lock);
	if (state === null || !state.held) return { kind: 'spawn' };
	const holder = parseLockJson(state.contents);
	const pid = holder.pid ?? 0;
	if (pid === 0) {
		return { kind: 'stop-or-quit', pid: 0, detail: `engine lock ${lock} is held but does not name a pid` };
	}
	if (!pidCanAct(pid)) {
		return { kind: 'stop-or-quit', pid, detail: `engine lock ${lock} is held by dead or zombie pid ${pid}` };
	}
	const port = holder.port ?? 0;
	if (port === 0 || !(await healthCheck(port))) {
		return {
			kind: 'stop-or-quit',
			pid,
			detail: `engine lock ${lock} is held by pid ${pid} but is not answering health`
		};
	}
	const host = holder.host !== null && holder.host !== '' ? holder.host : '127.0.0.1';
	return { kind: 'adopt', pid, host, port };
}

export function originForAdopt(host: string, port: number): string {
	return `http://${host}:${port}`;
}

function signalHolder(pid: number, signal: NodeJS.Signals): void {
	try {
		process.kill(-pid, signal);
	} catch {
		try {
			process.kill(pid, signal);
		} catch {
			// Already gone.
		}
	}
}

/** SIGTERM then SIGKILL a holder pid, using its group when it leads one. */
export async function stopHolderPid(pid: number): Promise<void> {
	appendShellLog('shutdown', `stopping adopted engine pid ${pid}: SIGTERM`);
	const sent = Date.now();
	signalHolder(pid, 'SIGTERM');
	while (Date.now() - sent < 5_000) {
		if (!pidAlive(pid)) {
			appendShellLog('shutdown', `adopted engine pid ${pid} exited ${Date.now() - sent}ms after SIGTERM`);
			return;
		}
		await sleep(50);
	}
	appendShellLog(
		'shutdown',
		`adopted engine pid ${pid} did not exit within ${Date.now() - sent}ms of SIGTERM; escalating to SIGKILL`
	);
	signalHolder(pid, 'SIGKILL');
	const killed = Date.now();
	while (pidAlive(pid) && Date.now() - killed < 10_000) await sleep(50);
	appendShellLog(
		pidAlive(pid) ? 'ERROR' : 'shutdown',
		pidAlive(pid) ? `adopted engine pid ${pid} still present 10s after SIGKILL` : `adopted engine pid ${pid} gone after SIGKILL`
	);
}
