// The one place the shell asks "which OS am I starting the engine on".
//
// Boundaries stay identical on every OS (ADR-NEW-electron-desktop-shell,
// ADR-NEW-portable-by-default-not-a-rust-rewrite): the shell starts one
// launcher with `--data-dir D --host 127.0.0.1 --port P`. Only the launcher's
// file name, the interpreter's path inside the payload and the way a script
// launcher is invoked differ, and they differ here and nowhere else.
//
// [if] a caller picks the launcher, interpreter or invocation without this module [then] the OS split leaks into the shell, [else stop]

import * as fs from 'node:fs';
import * as path from 'node:path';

import { EngineError } from './shell-log';

/** Payload-relative launcher: a shell script on POSIX, a batch file on Windows. */
export function engineLauncher(platform: NodeJS.Platform = process.platform): string {
	return platform === 'win32' ? 'bin/opendj-engine.cmd' : 'bin/opendj-engine';
}

/**
 * The payload's own interpreter, used by the lock probe. The Windows layout is
 * the contract the Windows payload recipe has to meet: `runtime/python.exe`.
 */
export function payloadPython(payloadDir: string, platform: NodeJS.Platform = process.platform): string {
	return platform === 'win32' ? path.join(payloadDir, 'runtime', 'python.exe') : path.join(payloadDir, 'runtime', 'bin', 'python3');
}

/**
 * A System32 tool under the system root the OS sets, or null when the environment
 * names no absolute root or the file is not there. Resolved by path, never looked
 * up on PATH, so a sanitized or hostile PATH cannot hide or replace it.
 */
export function windowsSystemTool(
	file: string,
	env: NodeJS.ProcessEnv = process.env,
	exists: (candidate: string) => boolean = fs.existsSync
): string | null {
	const root = env.SystemRoot ?? env.SYSTEMROOT ?? env.windir ?? env.WINDIR;
	if (!root || !path.win32.isAbsolute(root)) return null;
	const tool = path.win32.join(root, 'System32', file);
	return exists(tool) ? tool : null;
}

export interface LaunchInvocation {
	command: string;
	args: string[];
	windowsVerbatimArguments: boolean;
}

function isBatchFile(file: string): boolean {
	return /\.(cmd|bat)$/i.test(file);
}

/**
 * How to start `launcher` with `args`. A POSIX launcher runs directly. Node
 * refuses to spawn a `.cmd` directly on Windows (CVE-2024-27980), so it goes
 * through `cmd.exe /d /s /c "..."` with each token wrapped in double quotes.
 * Inside double quotes cmd.exe still expands `%VAR%` and cannot carry a literal
 * `"`, so a token holding either is refused by name instead of being mangled.
 */
export function launchInvocation(
	launcher: string,
	args: string[],
	platform: NodeJS.Platform = process.platform,
	resolveCmd: () => string | null = () => windowsSystemTool('cmd.exe')
): LaunchInvocation {
	if (platform !== 'win32' || !isBatchFile(launcher)) {
		return { command: launcher, args, windowsVerbatimArguments: false };
	}
	for (const token of [launcher, ...args]) {
		if (token.includes('"') || token.includes('%')) {
			throw new EngineError(
				'Open DJ cannot start its engine from this location.',
				`The path or argument ${token} contains a percent sign or a double quote, which Windows would reinterpret. Move the app or its data folder to a path without them.`
			);
		}
	}
	const cmd = resolveCmd();
	if (cmd === null) {
		throw new EngineError(
			'Open DJ cannot start its engine on this system.',
			'No cmd.exe under an absolute SystemRoot or windir, and the launcher is a batch file that only cmd.exe can run.'
		);
	}
	const line = [launcher, ...args].map((token) => `"${token}"`).join(' ');
	return { command: cmd, args: ['/d', '/s', '/c', `"${line}"`], windowsVerbatimArguments: true };
}
