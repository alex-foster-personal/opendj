// The bundled engine, supervised by the shell.
//
// Port of apps/desktop/src-tauri/src/engine.rs, rule for rule:
//
// 1. The port is PREFERRED, never required: PREFERRED_ENGINE_PORT when free,
//    otherwise an OS-assigned loopback port.
// 2. The data directory is this app's own data directory, never the repo's
//    data/ and never a real rekordbox library.
// 3. Rekordbox writeback is actively unset before the engine is spawned.
// 4. A boot that does not reach a healthy /api/v1/health inside the timeout
//    raises a native error naming the failure. No blank window.
// 5. Engine output is never silently lost: the log is proved writable before
//    the spawn, and a pump that dies stops the engine.
//
// The Rust audio engine (`odj-audio`) is not launched here: the Python engine
// supervises it, so both shells behave the same. The shell only tells the
// engine where the bundled binary is, in ODJ_AUDIO_BIN (ADR NEW
// rust-audio-engine-process, decision D5).

import { spawn as spawnChild, spawnSync, type ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as net from 'node:net';
import * as path from 'node:path';
import type { Readable } from 'node:stream';

import { appendRotated, ENGINE_LOG_MAX_BYTES, rotateLogIfNeeded } from './engine-log';
import { HEALTH_PATH } from './shell-health';
import { appendShellLog, EngineError, verifyLogWritable } from './shell-log';

export { EngineError } from './shell-log';

/** The payload directory inside the app's resources. */
export const PAYLOAD_DIR = 'payload';
/** The one entry point the shell knows. */
export const ENGINE_LAUNCHER = 'bin/opendj-engine';
/** Measured cold boots are ~1.5 s; 30 s is a bound, not a wait. */
export const BOOT_TIMEOUT_MS = 30_000;
export { HEALTH_PATH };
const POLL_INTERVAL_MS = 150;
const SOCKET_TIMEOUT_MS = 750;
/**
 * How long a stopping engine gets between SIGTERM and SIGKILL: the Tauri
 * shell's `SHUTDOWN_GRACE` (engine.rs), and for the same reason. Job workers
 * lead their own sessions, so the group SIGKILL never reaches them; a SIGKILL
 * that lands before the engine has reaped them leaves them running after the
 * app is gone (demon-llama, Fri 2 Oct 2026). The engine's worst-case shutdown
 * must fit inside it: (private path). A bound, not
 * a wait: an engine that exits sooner is reaped sooner.
 */
export const SHUTDOWN_GRACE_MS = 25_000;

/**
 * Environment the shell strips before spawning the engine. A build launched
 * from a developer's terminal inherits that terminal's .env: MDT_DATA_DIR
 * would point the installed app at a worktree's library, and
 * MDT_REKORDBOX_WRITEBACK_ENABLED would arm a path the app must never arm.
 * WEB_CONCURRENCY makes the engine refuse to boot at all.
 */
export const STRIPPED_ENV = [
	'MDT_DATA_DIR',
	'MDT_REKORDBOX_WRITEBACK_ENABLED',
	'MUSIC_DJ_STATE_BACKEND',
	'WEB_CONCURRENCY',
	// Set from the payload below or not at all: a developer's terminal must not
	// point the app's engine at a repo build of the audio engine.
	'ODJ_AUDIO_BIN'
] as const;

/** Where the payload bundles the Rust audio engine, when it does. */
export const AUDIO_ENGINE_BIN = 'bin/odj-audio';
export const AUDIO_ENGINE_BIN_ENV = 'ODJ_AUDIO_BIN';

/**
 * `{ODJ_AUDIO_BIN: <path>}` when the payload carries an executable audio
 * engine, otherwise nothing: the engine then keeps playback in the page, and
 * says so itself. The shell never falls back to another binary.
 */
export function audioEngineEnv(payloadDir: string, platform: NodeJS.Platform = process.platform): Record<string, string> {
	const binary = path.join(payloadDir, AUDIO_ENGINE_BIN + (platform === 'win32' ? '.exe' : ''));
	try {
		fs.accessSync(binary, platform === 'win32' ? fs.constants.F_OK : fs.constants.X_OK);
	} catch {
		return {};
	}
	return { [AUDIO_ENGINE_BIN_ENV]: binary };
}

export const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

// ----- port ---------------------------------------------------------------
/**
 * The port the packaged engine asks for first, so the browser address
 * (`http://opendj.localhost:7700/`) survives a relaunch.
 *
 * Chosen to sit outside every port this repo hands out or binds: below the
 * worktree pools (backend 8680+, frontend 9400+, every lane shifts upward),
 * clear of the dev defaults 8585/5173, of every fixed port in
 * `(private path)`, and of every e2e suite port; it is itself listed
 * in `RESERVED_FIXED_PORTS` (`apps/webui/port_config.py`) so the worktree
 * allocator never hands it out. Also clear of macOS's own AirPlay receiver
 * (5000/7000). Mirrors `PREFERRED_ENGINE_PORT` in
 * `apps/desktop/src-tauri/src/engine.rs`.
 */
export const PREFERRED_ENGINE_PORT = 7700;

const FALLBACK_PORT_TRIES = 20;

function listenAndClose(host: string, port: number): Promise<void> {
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.once('error', reject);
		server.listen(port, host, () => {
			server.close((error) => (error ? reject(error) : resolve()));
		});
	});
}

/**
 * Free means the engine's own bind (127.0.0.1) would succeed AND nobody
 * holds the port on ::1. The second half matters because macOS resolves
 * `opendj.localhost` to ::1 first: a stranger on [::1]:port would answer
 * the browser before the engine did. A host with no IPv6 loopback cannot
 * bind ::1 at all, which is not "in use" (EADDRNOTAVAIL and any other
 * ::1 error besides EADDRINUSE counts as not taken).
 */
export async function loopbackPortIsFree(port: number): Promise<boolean> {
	try {
		await listenAndClose('127.0.0.1', port);
	} catch {
		return false;
	}
	try {
		await listenAndClose('::1', port);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'EADDRINUSE') return false;
	}
	return true;
}

/**
 * `preferred` when it is free on loopback right now, else an OS-assigned
 * port that also passes `loopbackPortIsFree`. A fallback is written to the
 * shell log with both ports first (UIURL-01). A free preferred port is not
 * logged. Never fails because the preferred port is taken.
 */
export async function pickLoopbackPort(preferred: number): Promise<number> {
	if (await loopbackPortIsFree(preferred)) return preferred;
	for (let attempt = 0; attempt < FALLBACK_PORT_TRIES; attempt++) {
		const candidate = await freeLoopbackPort();
		if (await loopbackPortIsFree(candidate)) {
			appendShellLog(
				'INFO',
				`preferred engine port ${preferred} is in use; using OS-assigned port ${candidate}`
			);
			return candidate;
		}
	}
	throw new EngineError(
		'Open DJ could not reserve a local port.',
		`Could not find a free loopback port after ${FALLBACK_PORT_TRIES} attempts.`
	);
}

/** The engine's port: the preferred one when nobody holds it, otherwise one the OS assigns. */
export function enginePort(): Promise<number> {
	return pickLoopbackPort(PREFERRED_ENGINE_PORT);
}

/** Ask the OS for a loopback port nobody is using right now. */
export function freeLoopbackPort(): Promise<number> {
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.once('error', (error) =>
			reject(new EngineError('Open DJ could not reserve a local port.', `Binding 127.0.0.1:0 failed: ${error.message}`))
		);
		server.listen(0, '127.0.0.1', () => {
			const address = server.address();
			server.close(() => {
				if (address === null || typeof address === 'string') {
					reject(new EngineError('Open DJ could not read back the port it reserved.', String(address)));
				} else {
					resolve(address.port);
				}
			});
		});
	});
}

// ----- health -------------------------------------------------------------
/**
 * One loopback GET, status line only. Deliberately a raw socket, as in the
 * Rust shell: no HTTP client, no proxy settings, no redirects, one line read.
 */
export function healthOk(port: number, host = '127.0.0.1'): Promise<boolean> {
	return new Promise((resolve) => {
		let settled = false;
		let response = '';
		const finish = (ok: boolean): void => {
			if (settled) return;
			settled = true;
			socket.destroy();
			resolve(ok);
		};
		const socket = net.connect({ host, port });
		socket.setNoDelay(true);
		socket.setTimeout(SOCKET_TIMEOUT_MS, () => finish(false));
		socket.once('error', () => finish(false));
		socket.once('connect', () => {
			socket.write(
				`GET ${HEALTH_PATH} HTTP/1.1\r\nHost: ${host}:${port}\r\n` +
					'Connection: close\r\nAccept: application/json\r\n\r\n'
			);
		});
		socket.on('data', (chunk: Buffer) => {
			response += chunk.toString('latin1');
			const newline = response.indexOf('\n');
			if (newline !== -1 || response.length >= 4096) {
				finish(response.startsWith('HTTP/1.1 200'));
			}
		});
		socket.once('end', () => finish(response.startsWith('HTTP/1.1 200')));
	});
}

// ----- build profile ------------------------------------------------------
/** Mirrors apps/feature_flags/profiles.py. */
export const APPSTORE_PROFILE = 'appstore';
const SANDBOX_CONTAINER_ENV = 'APP_SANDBOX_CONTAINER_ID';
const SANDBOX_HOME_MARKER = '/Library/Containers/';

/**
 * Which `--build-profile` this boot passes, or null for the full build.
 * Detected at runtime (SAND-04): either signal alone trips it, because a
 * false positive costs a readable refusal and a false negative costs a
 * silently empty USB drive list. macOS-only, passed in so Linux CI tests it.
 */
export function buildProfileFor(
	containerId: string | undefined,
	home: string | undefined,
	hostIsMacos: boolean
): string | null {
	if (!hostIsMacos) return null;
	const containerSet = containerId !== undefined && containerId.trim() !== '';
	const homeInContainer = home !== undefined && home.includes(SANDBOX_HOME_MARKER);
	return containerSet || homeInContainer ? APPSTORE_PROFILE : null;
}

function buildProfile(): string | null {
	return buildProfileFor(process.env[SANDBOX_CONTAINER_ENV], process.env.HOME, process.platform === 'darwin');
}

// ----- log sink -----------------------------------------------------------
/**
 * The engine's log file, and the one place a pump failure is recorded. Node
 * runs both pumps on one thread, so appends cannot interleave with a rotation
 * and no lock is needed; the first failure recorded wins.
 */
export class LogSink {
	private failureDetail: string | null = null;

	constructor(readonly path: string) {}

	append(bytes: Buffer): void {
		appendRotated(this.path, bytes);
	}

	recordFailure(detail: string): void {
		appendShellLog('ERROR', detail);
		if (this.failureDetail === null) this.failureDetail = detail;
	}

	failure(): string | null {
		return this.failureDetail;
	}
}

/**
 * Copy one stream into the log until it ends, or reject saying why it stopped.
 * Every read error and every append or rotation error is terminal and is
 * returned, because the caller is what turns it into a stopped engine.
 */
export function pumpStream(stream: Readable, streamName: string, sink: LogSink): Promise<void> {
	return new Promise((resolve, reject) => {
		let failed = false;
		const fail = (detail: string): void => {
			if (failed) return;
			failed = true;
			stream.removeAllListeners('data');
			stream.resume();
			reject(new Error(detail));
		};
		stream.on('data', (chunk: Buffer | string) => {
			if (failed) return;
			try {
				sink.append(typeof chunk === 'string' ? Buffer.from(chunk) : chunk);
			} catch (error) {
				fail(`could not write engine ${streamName} to ${sink.path}: ${(error as Error).message}`);
			}
		});
		stream.once('error', (error) => fail(`could not read engine ${streamName}: ${error.message}`));
		stream.once('end', () => {
			if (!failed) resolve();
		});
	});
}

// ----- process group ------------------------------------------------------
/**
 * Where Windows keeps taskkill.exe, from the system root the OS sets, or null when the
 * environment names no absolute root or the file is not there. Resolved by path, never
 * looked up on PATH, so a sanitized or hostile PATH cannot hide or replace it (Codex on #6830).
 */
export function windowsTaskkill(
	env: NodeJS.ProcessEnv = process.env,
	exists: (file: string) => boolean = fs.existsSync
): string | null {
	const root = env.SystemRoot ?? env.SYSTEMROOT ?? env.windir ?? env.WINDIR;
	if (!root || !path.win32.isAbsolute(root)) return null;
	const taskkill = path.win32.join(root, 'System32', 'taskkill.exe');
	return exists(taskkill) ? taskkill : null;
}

/**
 * taskkill.exe could not be found, so a Windows process tree could not be signalled at all.
 * Distinct from `signalGroup` returning false: the tree may well still be running (Sol P1 on #6830).
 */
export class TaskkillUnavailableError extends Error {}

/** A shutdown that stopped the engine's leader but could not prove its helpers stopped. */
export class EngineCleanupError extends EngineError {}

/**
 * Signal a process group this shell created. Returns false if nothing was there, or (on
 * Windows) if taskkill /T did not succeed. Throws `TaskkillUnavailableError` when Windows
 * has no taskkill.exe to ask, since that says nothing about whether the tree is there.
 */
export function signalGroup(
	pid: number,
	signal: NodeJS.Signals,
	platform: NodeJS.Platform = process.platform,
	resolveTaskkill: () => string | null = windowsTaskkill
): boolean {
	if (platform === 'win32') {
		// Windows has no process groups; taskkill /T walks the child tree.
		const taskkill = resolveTaskkill();
		if (taskkill === null) {
			throw new TaskkillUnavailableError(
				`cannot ${signal} process tree ${pid}: no taskkill.exe under an absolute SystemRoot/windir`
			);
		}
		const args = ['/PID', String(pid), '/T'];
		if (signal === 'SIGKILL') args.push('/F');
		return spawnSync(taskkill, args, { stdio: 'ignore' }).status === 0;
	}
	try {
		process.kill(-pid, signal);
		return true;
	} catch {
		return false;
	}
}

/** Whether any process holds this pid; EPERM still means it exists. */
function pidExists(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code === 'EPERM';
	}
}

/**
 * SIGKILL whatever is left in a group whose leader was just reaped, as Tauri's
 * `sweep_orphaned_group` does. The kernel does not reissue a pid while a group of
 * that number exists, so a live process at the leader's pid proves our group is
 * gone and the number now belongs to someone else: refuse. Otherwise the group is
 * ours or empty, and an empty group is ESRCH. POSIX only: Windows has no groups,
 * so its tree sweep is separate work.
 */
export function sweepOrphanedGroup(
	pgid: number,
	name: string,
	platform: NodeJS.Platform = process.platform,
	pidIsLive: (pid: number) => boolean = pidExists,
	send: (pid: number, signal: NodeJS.Signals) => boolean = (target, signal) => signalGroup(target, signal, platform)
): void {
	if (platform === 'win32' || pgid <= 0) return;
	if (pidIsLive(pgid)) {
		appendShellLog('shutdown', `${name} pgid ${pgid}: that pid is in use again, so the group is not ours; not sweeping`);
		return;
	}
	if (send(pgid, 'SIGKILL')) {
		appendShellLog('shutdown', `${name} pgid ${pgid}: SIGKILLed processes left behind by the exited ${name}`);
	}
}

/** SIGTERM a process group this shell created at spawn. */
export function stopProcessGroup(pgid: number): void {
	signalGroup(pgid, 'SIGTERM');
}

export interface ExitInfo {
	code: number | null;
	signal: NodeJS.Signals | null;
}

export interface ProcessSpec {
	/** Human name used in log lines and errors ("engine", "audio-engine"). */
	name: string;
	launcher: string;
	dataDir: string;
	logPath: string;
	port: number;
	env?: Record<string, string>;
	args?: string[];
}

/** A spawned engine and the address it was told to serve on. */
export class Engine {
	private exit: ExitInfo | null = null;
	private readonly exited: Promise<ExitInfo>;

	constructor(
		readonly child: ChildProcess,
		readonly port: number,
		readonly dataDir: string,
		readonly logPath: string,
		readonly sink: LogSink,
		readonly name = 'engine'
	) {
		this.exited = new Promise((resolve) => {
			const record = (code: number | null, signal: NodeJS.Signals | null): void => {
				if (this.exit === null) {
					this.exit = { code, signal };
					sweepOrphanedGroup(this.pid, this.name);
				}
				resolve(this.exit);
			};
			if (child.exitCode !== null || child.signalCode !== null) {
				record(child.exitCode, child.signalCode);
			} else {
				child.once('exit', record);
			}
		});
	}

	origin(): string {
		return `http://127.0.0.1:${this.port}`;
	}

	get pid(): number {
		return this.child.pid ?? 0;
	}

	logFailure(): string | null {
		return this.sink.failure();
	}

	isLogFailed(): boolean {
		return this.logFailure() !== null;
	}

	/** Non-blocking: the exit status once the child has exited. Node reaps for us. */
	tryReap(): ExitInfo | null {
		return this.exit;
	}

	waitReap(): Promise<ExitInfo> {
		return this.exited;
	}

	/** Wait for the engine to answer its own health route. */
	async waitUntilHealthy(timeoutMs: number, signal?: AbortSignal): Promise<void> {
		const deadline = Date.now() + timeoutMs;
		for (;;) {
			if (signal?.aborted) {
				throw new EngineError(
					`Open DJ stopped waiting for the ${this.name}.`,
					`Quit was requested while the ${this.name} was starting.\nData directory: ${this.dataDir}\nEngine log: ${this.logPath}`
				);
			}
			// BEFORE the exit check: a pump failure stops the engine, and asked
			// the other way round this would report a bare exit status.
			const lost = this.logFailure();
			if (lost !== null) {
				throw new EngineError(
					`Open DJ lost the ${this.name} log.`,
					`${lost}\nThe ${this.name} was stopped rather than left running with nothing recording it.\n` +
						`Data directory: ${this.dataDir}\nEngine log: ${this.logPath}`
				);
			}
			if (this.exit !== null) {
				throw new EngineError(
					`The Open DJ ${this.name} stopped while starting up.`,
					`It exited with ${describeExit(this.exit)} before answering ${HEALTH_PATH}.\n` +
						`Data directory: ${this.dataDir}\nEngine log: ${this.logPath}`
				);
			}
			if (await healthOk(this.port)) return;
			if (Date.now() >= deadline) {
				throw new EngineError(
					`The Open DJ ${this.name} did not finish starting.`,
					`It is still running but did not answer http://127.0.0.1:${this.port}${HEALTH_PATH} ` +
						`within ${Math.round(timeoutMs / 1000)}s.\nData directory: ${this.dataDir}\nEngine log: ${this.logPath}`
				);
			}
			await sleep(POLL_INTERVAL_MS);
		}
	}

	/** How the tree is signalled; replaceable so a test can play Windows' taskkill results. */
	treeSignals: { platform: NodeJS.Platform; send: (pid: number, signal: NodeJS.Signals) => boolean } = {
		platform: process.platform,
		send: (pid, signal) => signalGroup(pid, signal)
	};

	/**
	 * Terminate the engine AND anything it spawned: SIGTERM to the group, the
	 * SHUTDOWN_GRACE_MS grace, then SIGKILL, each logged distinctly (#2801). On Windows a
	 * taskkill /T that cannot ask the tree to close goes straight to /F, and one
	 * that cannot force it either is a logged cleanup failure: the leader is killed
	 * alone so quitting never waits on a tree nothing could reach (Sol P1 on #6830).
	 */
	async shutdown(graceMs: number = SHUTDOWN_GRACE_MS): Promise<void> {
		if (this.exit !== null) return;
		const pid = this.pid;
		appendShellLog('shutdown', `stopping ${this.name} pgid ${pid}: SIGTERM`);
		const sent = Date.now();
		const termed = this.signalTree(pid, 'SIGTERM');
		if (termed === 'unavailable') return this.stopLeaderAlone(pid);
		if (termed === 'sent') {
			const graceful = await Promise.race([this.exited.then(() => true), sleep(graceMs).then(() => false)]);
			if (graceful) {
				appendShellLog('shutdown', `${this.name} pgid ${pid} exited ${Date.now() - sent}ms after SIGTERM`);
				return;
			}
			appendShellLog(
				'shutdown',
				`${this.name} pgid ${pid} did not exit within ${Date.now() - sent}ms of SIGTERM; escalating to SIGKILL`
			);
		} else {
			appendShellLog('shutdown', `taskkill /T could not ask ${this.name} pid ${pid}'s tree to close; forcing it`);
		}
		if (this.signalTree(pid, 'SIGKILL') !== 'sent') return this.stopLeaderAlone(pid);
		await this.exited;
		appendShellLog('shutdown', `${this.name} pgid ${pid} reaped after SIGKILL`);
	}

	/**
	 * Signal the engine's tree: 'sent'; 'failed' when Windows' taskkill /T did not succeed
	 * (on POSIX a false only means the group is already gone, which counts as sent); or
	 * 'unavailable' when Windows has no taskkill.exe at all.
	 */
	private signalTree(pid: number, signal: NodeJS.Signals): 'sent' | 'failed' | 'unavailable' {
		try {
			if (this.treeSignals.send(pid, signal)) return 'sent';
			return this.treeSignals.platform === 'win32' ? 'failed' : 'sent';
		} catch (error) {
			if (!(error instanceof TaskkillUnavailableError)) throw error;
			appendShellLog('shutdown', `[ERROR] ${error.message}`);
			return 'unavailable';
		}
	}

	/**
	 * The tree could not be stopped: kill the leader itself so nothing waits on it, then
	 * fail. Helpers may still hold the port or the data directory, so this is never a
	 * clean shutdown, and every caller sees it as a rejection (Sol P1 on #6830).
	 */
	private async stopLeaderAlone(pid: number): Promise<never> {
		const detail = `Could not stop ${this.name} pid ${pid}'s process tree; killed it alone, and its helpers may still be running.`;
		appendShellLog('shutdown', `[ERROR] ${detail}`);
		this.child.kill('SIGKILL');
		await this.exited;
		throw new EngineCleanupError(`The Open DJ ${this.name} did not stop cleanly.`, detail);
	}
}

export function describeExit(exit: ExitInfo): string {
	if (exit.signal !== null) return `signal ${exit.signal}`;
	if (exit.code !== null) return `exit status ${exit.code}`;
	return 'an unknown status';
}

export function logBootId(): string {
	return `shell-${process.pid}-${Date.now()}`;
}

/**
 * Start a supervised process with the engine contract:
 * `<launcher> --data-dir <dir> --host 127.0.0.1 --port <port> [args...]`,
 * its own process group, stdout and stderr pumped into `logPath`.
 */
export function spawnProcess(spec: ProcessSpec): Engine {
	const { name, launcher, dataDir, logPath, port } = spec;
	let isFile = false;
	try {
		isFile = fs.statSync(launcher).isFile();
	} catch {
		isFile = false;
	}
	if (!isFile) {
		throw new EngineError(
			name === 'engine' ? 'This Open DJ build has no engine inside it.' : `This Open DJ build has no ${name} inside it.`,
			`Expected it at ${launcher}. The app was assembled without its payload; reinstall from a complete build.`
		);
	}
	for (const [dir, headline] of [
		[dataDir, 'Open DJ could not create its data folder.'],
		[path.dirname(logPath), 'Open DJ could not create its log folder.']
	] as const) {
		try {
			fs.mkdirSync(dir, { recursive: true });
		} catch (error) {
			throw new EngineError(headline, `${dir}: ${(error as Error).message}`);
		}
	}
	try {
		rotateLogIfNeeded(logPath, ENGINE_LOG_MAX_BYTES);
	} catch (error) {
		throw new EngineError(`Open DJ could not rotate its ${name} log.`, `${logPath}: ${(error as Error).message}`);
	}
	// BEFORE the spawn: an unwritable log is a launch-time fact.
	verifyLogWritable(logPath);

	// Strip what the parent inherited, then apply what the shell decided.
	const env: NodeJS.ProcessEnv = { ...process.env };
	for (const stripped of STRIPPED_ENV) delete env[stripped];
	Object.assign(env, spec.env);
	const args = ['--data-dir', dataDir, '--host', '127.0.0.1', '--port', String(port), ...(spec.args ?? [])];

	let child: ChildProcess;
	try {
		// detached: the child leads a new process group on POSIX, so one
		// signal reaches the jobs it spawns too.
		child = spawnChild(launcher, args, { env, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
	} catch (error) {
		throw new EngineError(`Open DJ could not start its ${name}.`, `Launching ${launcher} failed: ${(error as Error).message}`);
	}
	const sink = new LogSink(logPath);
	const engine = new Engine(child, port, dataDir, logPath, sink, name);
	child.once('error', (error) => sink.recordFailure(`could not start ${name}: ${error.message}`));
	const pgid = child.pid ?? 0;
	for (const [stream, streamName] of [
		[child.stdout, 'stdout'],
		[child.stderr, 'stderr']
	] as const) {
		if (stream === null) continue;
		pumpStream(stream, streamName, sink).catch((error: Error) => {
			sink.recordFailure(error.message);
			if (pgid > 0) stopProcessGroup(pgid);
		});
	}
	return engine;
}

/** Start the bundled Python engine on `port`. */
export function spawnEngine(payloadDir: string, dataDir: string, logPath: string, port: number): Engine {
	const args: string[] = [];
	// A sandboxed shell means an App Store build. Passed as an argument so it
	// survives STRIPPED_ENV and shows in the engine's own boot line.
	const profile = buildProfile();
	if (profile !== null) args.push('--build-profile', profile);
	return spawnProcess({
		name: 'engine',
		launcher: path.join(payloadDir, ENGINE_LAUNCHER),
		dataDir,
		logPath,
		port,
		args,
		env: {
			OPENDJ_ENGINE_WARN_LOG: path.join(path.dirname(logPath), 'engine-warn.log'),
			OPENDJ_ENGINE_LOG_BOOT_ID: logBootId(),
			OPENDJ_PARENT_PID: String(process.pid),
			...audioEngineEnv(payloadDir)
		}
	});
}
