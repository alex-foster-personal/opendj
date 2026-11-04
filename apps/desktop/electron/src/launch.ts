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

import {
	SHUTDOWN_GRACE_MS,
	signalGroup,
	sleep,
	sweepOrphanedGroup,
	TaskkillUnavailableError
} from './engine';
import { appendShellLog } from './shell-log';

const LOCK_FILE_NAME = '.engine.lock';
const LOCK_READ_ATTEMPTS = 5;
const LOCK_READ_RETRY_MS = 10;

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
	// Windows has no zombie state to read, and no ps: a bare name would only find a shadow.
	if (process.platform === 'win32') return null;
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

/** Parsed lock JSON, or null when the body is empty or not a JSON object. */
export function tryParseLockJson(raw: string): LockJson | null {
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return null;
	}
	if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
	const obj = value as Record<string, unknown>;
	const uint = (field: unknown): number | null =>
		typeof field === 'number' && Number.isInteger(field) && field >= 0 ? field : null;
	return {
		pid: uint(obj.pid),
		host: typeof obj.host === 'string' ? obj.host : null,
		port: uint(obj.port)
	};
}

export function parseLockJson(raw: string): LockJson {
	return tryParseLockJson(raw) ?? { pid: null, host: null, port: null };
}

function sleepMs(ms: number): void {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		// Busy-wait: sync reads must not depend on timers in tests.
	}
}

/** Retry unparsable reads; for tests pass a queue via `readRaw`. */
export function readLockJsonWithAttempts(readRaw: () => string | null): LockJson | null {
	for (let attempt = 0; attempt < LOCK_READ_ATTEMPTS; attempt++) {
		const raw = readRaw();
		if (raw === null) return null;
		const parsed = tryParseLockJson(raw);
		if (parsed !== null) return parsed;
		if (attempt + 1 < LOCK_READ_ATTEMPTS) sleepMs(LOCK_READ_RETRY_MS);
	}
	return null;
}

function readLockJsonFromPath(lock: string): LockJson | null {
	return readLockJsonWithAttempts(() => {
		try {
			return fs.readFileSync(lock, 'utf8');
		} catch {
			return null;
		}
	});
}

/** The holder's pid and port, or null when the lock does not name both. */
export function readLockFields(lock: string): { pid: number; port: number } | null {
	const holder = readLockJsonFromPath(lock);
	if (!holder?.pid || !holder?.port) return null;
	return { pid: holder.pid, port: holder.port };
}

/** What a lock probe reports: whether the flock is held, and the holder's JSON. */
export interface LockProbe {
	held: boolean;
	contents: string;
	/** How "held" was established, for the log and the dialog. */
	method: 'flock' | 'pid-liveness';
}

// The same call apps/engine_core/lock.py makes, from the other side, through the
// one seam apps/shared/file_lock.py has: flock on POSIX, msvcrt.locking of the
// byte at 1 << 30 on Windows. Held means another process owns it. Any other
// failure on Windows exits non-zero, so the caller falls back to pid liveness
// instead of guessing.
const FLOCK_PROBE = [
	'import errno, json, os, sys',
	'fd = os.open(sys.argv[1], os.O_RDWR)',
	'if sys.platform == "win32":',
	'    import msvcrt',
	'    os.lseek(fd, 1 << 30, 0)',
	'    try:',
	'        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)',
	'        held = False',
	'        os.lseek(fd, 1 << 30, 0)',
	'        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)',
	'    except OSError as exc:',
	'        if exc.errno != errno.EACCES:',
	'            raise',
	'        held = True',
	'else:',
	'    import fcntl',
	'    try:',
	'        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)',
	'        held = False',
	'        fcntl.flock(fd, fcntl.LOCK_UN)',
	'    except OSError:',
	'        held = True',
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

function readLockContentsRetrying(lock: string, initial: string): LockJson | null {
	const first = tryParseLockJson(initial);
	if (first !== null) return first;
	return readLockJsonWithAttempts(() => {
		try {
			return fs.readFileSync(lock, 'utf8');
		} catch {
			return null;
		}
	});
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
	const holder = readLockContentsRetrying(lock, state.contents);
	if (holder === null) {
		return {
			kind: 'stop-or-quit',
			pid: 0,
			detail: `engine lock ${lock} is held but does not name a pid`
		};
	}
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

/**
 * The page the shell opens on the engine. `OPENDJ_PERF_PROBE=1` adds
 * `?perfprobe=1`, which turns on the in-page frame probe (PERFPROBE-02):
 * packaged builds have no devtools, so this is the only way to opt in there.
 */
export function performancePageUrl(origin: string, env: NodeJS.ProcessEnv = process.env): string {
	return env.OPENDJ_PERF_PROBE === '1' ? `${origin}/performance?perfprobe=1` : `${origin}/performance`;
}

/**
 * Signal an adopted engine and its descendants. Windows has no groups, so the tree
 * goes through the same taskkill seam a spawned engine's shutdown uses; with no
 * taskkill.exe the leader alone is killed, logged, so quitting never hangs on it.
 */
export function signalHolder(
	pid: number,
	signal: NodeJS.Signals,
	platform: NodeJS.Platform = process.platform,
	signalTree: (pid: number, signal: NodeJS.Signals) => boolean = (target, sig) => signalGroup(target, sig, platform)
): void {
	if (platform === 'win32') {
		try {
			signalTree(pid, signal);
			return;
		} catch (error) {
			if (!(error instanceof TaskkillUnavailableError)) throw error;
			appendShellLog('ERROR', `adopted engine pid ${pid}: ${error.message}; stopping the leader alone`);
		}
		try {
			process.kill(pid, signal);
		} catch {
			// Already gone.
		}
		return;
	}
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

/** SIGTERM then SIGKILL a holder pid, using its group when it leads one, with the spawned engine's grace. */
export async function stopHolderPid(pid: number, graceMs: number = SHUTDOWN_GRACE_MS): Promise<void> {
	appendShellLog('shutdown', `stopping adopted engine pid ${pid}: SIGTERM`);
	const sent = Date.now();
	signalHolder(pid, 'SIGTERM');
	while (Date.now() - sent < graceMs) {
		if (!pidAlive(pid)) {
			appendShellLog('shutdown', `adopted engine pid ${pid} exited ${Date.now() - sent}ms after SIGTERM`);
			sweepOrphanedGroup(pid, 'adopted engine');
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
