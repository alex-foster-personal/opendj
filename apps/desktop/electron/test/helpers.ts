// Shared fixtures for the shell's node:test suite. Everything here is real:
// real directories, real processes, real sockets. Nothing is mocked.

import * as fs from 'node:fs';
import * as http from 'node:http';
import * as os from 'node:os';
import { spawnSync } from 'node:child_process';
import * as path from 'node:path';

import { engineLauncher, windowsSystemTool } from '../src/platform';

/** Repo root for subprocesses that import `apps.*` (compiled tests live under dist-test/). */
export const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..', '..');

/** Repo venv interpreter for subprocesses that import `apps.*`. */
export function resolveRepoPython(): string {
	const venvRel = process.platform === 'win32' ? path.join('Scripts', 'python.exe') : path.join('bin', 'python');
	const venvPython = path.join(REPO_ROOT, '.venv', venvRel);
	if (fs.existsSync(venvPython)) return venvPython;
	throw new Error(`missing repo venv at ${venvPython}; run uv sync in ${REPO_ROOT}`);
}

/** The fixture sources live beside the TS tests, not in the compiled tree. */
export const FIXTURES = path.resolve(__dirname, '..', '..', 'test', 'fixtures');
export const FAKE_ENGINE = path.join(FIXTURES, 'fake-engine.js');

/**
 * The absolute path of `name` on PATH, resolved once here so every launch runs a
 * resolved program, never a bare name (AGENTS.md). Throws when it is not found.
 */
export function resolveProgram(name: string, env: NodeJS.ProcessEnv = process.env): string {
	const exts = process.platform === 'win32' ? (env.PATHEXT ?? '.EXE;.CMD;.BAT').split(';').filter(Boolean) : [''];
	for (const dir of (env.PATH ?? env.Path ?? '').split(path.delimiter)) {
		if (!dir || !path.isAbsolute(dir)) continue;
		for (const ext of exts) {
			const candidate = path.join(dir, name + ext);
			try {
				if (fs.statSync(candidate).isFile()) return candidate;
			} catch {
				// Not in this directory.
			}
		}
	}
	throw new Error(`${name} not found on an absolute PATH entry`);
}

/** An interpreter that exists on every CI host: `python` on Windows (no python3), `python3` elsewhere, by resolved path. */
export const PYTHON = resolveProgram(process.platform === 'win32' ? 'python' : 'python3');

/**
 * Python source that holds the engine lock the way apps/shared/file_lock.py does
 * (flock, or msvcrt.locking of the byte at 1 << 30), prints `ready`, and sleeps.
 */
export const HOLD_LOCK_SCRIPT = [
	'import os, sys, time',
	'fd = os.open(sys.argv[1], os.O_RDWR)',
	'if sys.platform == "win32":',
	'    import msvcrt',
	'    os.lseek(fd, 1 << 30, 0)',
	'    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)',
	'else:',
	'    import fcntl',
	'    fcntl.flock(fd, fcntl.LOCK_EX)',
	'print("ready", flush=True)',
	'time.sleep(60)'
].join('\n');

/** Why a test is skipped on Windows: it asserts a POSIX-only behavior, named here. */
export const POSIX_ONLY = (what: string): string | false => (process.platform === 'win32' ? `${what} is POSIX only` : false);

/** Arguments for a child that sleeps `seconds` and needs no /bin/sleep. */
export function sleeperArgs(seconds: number): [string, string[]] {
	return [process.execPath, ['-e', `setTimeout(() => {}, ${seconds * 1000})`]];
}

/**
 * Kill an engine the way an outside force would, with no shutdown courtesy.
 * POSIX signals the child; Windows has no signals and the launcher is a batch
 * file wrapping the real process, so the whole tree goes (taskkill /T /F).
 */
export function killEngine(pid: number): void {
	if (process.platform === 'win32') {
		const taskkill = windowsSystemTool('taskkill.exe');
		if (!taskkill) throw new Error('taskkill.exe not found under SystemRoot\\System32');
		const result = spawnSync(taskkill, ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
		if (result.status !== 0) throw new Error(`taskkill /PID ${pid} /T /F failed: status ${result.status}, ${result.error ?? 'no error'}`);
	} else {
		process.kill(pid, 'SIGKILL');
	}
}

/** How the shell reports an engine killed from outside: by signal on POSIX, exit code 1 from taskkill /F on Windows. */
export const KILLED =
	process.platform === 'win32' ? { exit: 'code 1', code: '1' } : { exit: 'signal SIGKILL', code: '-1' };

export function scratchDir(name: string): string {
	return fs.mkdtempSync(path.join(os.tmpdir(), `opendj-electron-${name}-`));
}

/**
 * A payload whose `bin/opendj-engine` runs the fake engine under this Node, in
 * `mode`. Returns the payload dir.
 */
export function fakePayload(root: string, mode = 'healthy', launcher = engineLauncher()): string {
	const payload = path.join(root, 'payload');
	const file = path.join(payload, launcher);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	if (process.platform === 'win32') {
		fs.writeFileSync(file, `@echo off\r\nset FAKE_ENGINE_MODE=${mode}\r\n"${process.execPath}" "${FAKE_ENGINE}" %*\r\n`);
	} else {
		fs.writeFileSync(
			file,
			`#!/bin/sh\nFAKE_ENGINE_MODE=${mode} exec ${JSON.stringify(process.execPath)} ${JSON.stringify(FAKE_ENGINE)} "$@"\n`,
			{ mode: 0o755 }
		);
	}
	return payload;
}

/** A loopback server answering `status` on every path. Resolves with its port. */
export function statusServer(status: number): Promise<{ port: number; close: () => Promise<void> }> {
	const server = http.createServer((_req, res) => {
		res.writeHead(status, { 'content-type': 'application/json' });
		res.end('{}');
	});
	return new Promise((resolve) => {
		server.listen(0, '127.0.0.1', () => {
			const address = server.address() as { port: number };
			resolve({ port: address.port, close: () => new Promise((done) => server.close(() => done())) });
		});
	});
}

export function pidGone(pid: number): boolean {
	try {
		process.kill(pid, 0);
	} catch {
		return true;
	}
	// An unreaped zombie still answers signal 0 but is no longer running.
	try {
		return /^\d+ \(.*\) Z/.test(fs.readFileSync(`/proc/${pid}/stat`, 'utf8'));
	} catch {
		return false;
	}
}

export async function waitFor(predicate: () => boolean | Promise<boolean>, timeoutMs = 5_000): Promise<boolean> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await predicate()) return true;
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
	return predicate();
}
