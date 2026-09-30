// Shell-owned lines in the shared engine log.
//
// Port of the shell-log half of apps/desktop/src-tauri/src/engine.rs: one
// process-wide log path, `[shell LEVEL] message` lines, stderr when the log is
// not installed or has gone away, and uncaught errors routed into the same file.

import * as fs from 'node:fs';
import * as path from 'node:path';

import { appendRotated } from './engine-log';

export class EngineError extends Error {
	constructor(
		readonly headline: string,
		readonly detail: string
	) {
		super(`${headline}\n\n${detail}`);
		this.name = 'EngineError';
	}
}

let shellLogPath: string | null = null;

export function appendShellLogToPath(logPath: string, level: string, message: string): void {
	const line = `[shell ${level}] ${message}\n`;
	try {
		appendRotated(logPath, line);
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === 'ENOENT') {
			process.stderr.write(`[${level}] ${message}\n`);
			return;
		}
		process.stderr.write(`[shell log failed] ${String(error)}: [${level}] ${message}\n`);
	}
}

/** Append one shell-owned line to the shared engine log. */
export function appendShellLog(level: string, message: string): void {
	if (shellLogPath === null) {
		process.stderr.write(`[${level}] ${message}\n`);
		return;
	}
	appendShellLogToPath(shellLogPath, level, message);
}

/** Prove the engine log can be appended to, before anything depends on it. */
export function verifyLogWritable(logPath: string): void {
	try {
		fs.closeSync(fs.openSync(logPath, 'a'));
	} catch (error) {
		throw new EngineError(
			'Open DJ could not write its engine log.',
			`${logPath}: ${String((error as Error).message ?? error)}`
		);
	}
}

/** Test seam: the install rule without the process-wide hook. */
export function installShellLogPath(current: { path: string | null }, logPath: string): void {
	try {
		fs.mkdirSync(path.dirname(logPath), { recursive: true });
	} catch (error) {
		throw new EngineError(
			'Open DJ could not create its log folder.',
			`${path.dirname(logPath)}: ${String((error as Error).message ?? error)}`
		);
	}
	verifyLogWritable(logPath);
	if (current.path !== null) {
		throw new EngineError(
			'Open DJ shell logging is already installed.',
			`retained ${current.path} requested ${logPath}`
		);
	}
	current.path = logPath;
}

/** Route uncaught main-process errors into the same engine log the child uses. */
export function installShellLogging(logPath: string): void {
	const slot = { path: shellLogPath };
	installShellLogPath(slot, logPath);
	shellLogPath = slot.path;
	// The monitor variant observes without changing Electron's own handling,
	// which is what the Rust panic hook did: record, then let the crash happen.
	process.on('uncaughtExceptionMonitor', (error, origin) => {
		const detail = `${origin}: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`;
		appendShellLog('panic', detail);
		process.stderr.write(`[PANIC] ${detail}\n`);
	});
}

/** The tail of a log, for an error dialog that says something. */
export function logTail(logPath: string, lines: number): string {
	let text: string;
	try {
		text = fs.readFileSync(logPath, 'utf8');
	} catch {
		return '';
	}
	const collected = text.split('\n');
	if (collected.at(-1) === '') collected.pop();
	return collected.slice(Math.max(0, collected.length - lines)).join('\n');
}

/** UTC RFC3339 with millisecond precision and a `+00:00` suffix, as the Rust shell wrote. */
export function utcTimestampIso(now: Date = new Date()): string {
	return now.toISOString().replace(/Z$/, '+00:00');
}
