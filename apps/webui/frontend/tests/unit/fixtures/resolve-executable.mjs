/**
 * Resolve a program to its path before launching it (AGENTS.md "Portable by
 * default": programs are launched by resolved path, never a bare name).
 *
 * Searches PATH the way the OS would, with PATHEXT on Windows, and returns the
 * first regular file that can be executed. When nothing is found it throws
 * ExecutableNotFoundError naming the program and how many directories it
 * searched, so a test fails with that reason instead of a spawn ENOENT.
 */
import { accessSync, constants, statSync } from 'node:fs';
import path from 'node:path';

export class ExecutableNotFoundError extends Error {
	/**
	 * @param {string} name
	 * @param {number} searched
	 */
	constructor(name, searched) {
		super(
			`${name} was not found on PATH (searched ${searched} directories); ` +
				`install it or put it on PATH before running this test`
		);
		this.name = 'ExecutableNotFoundError';
		this.executable = name;
	}
}

/**
 * @param {string} name a program name with no directory, e.g. 'uv'
 * @param {NodeJS.ProcessEnv} [env]
 * @param {NodeJS.Platform} [platform]
 * @returns {string} the absolute path of the program
 */
export function resolveExecutable(name, env = process.env, platform = process.platform) {
	const windows = platform === 'win32';
	const pathValue = env.PATH ?? env.Path ?? '';
	const dirs = pathValue.split(windows ? ';' : ':').filter((dir) => dir.length > 0);
	const exts = windows
		? (env.PATHEXT ?? '.COM;.EXE;.BAT;.CMD').split(';').filter((ext) => ext.length > 0)
		: [''];
	for (const dir of dirs) {
		for (const ext of exts) {
			const candidate = path.resolve(dir, name + ext);
			if (!_isRegularFile(candidate)) continue;
			if (!windows && !_isExecutable(candidate)) continue;
			return candidate;
		}
	}
	throw new ExecutableNotFoundError(name, dirs.length);
}

/** ENOENT and ENOTDIR are the expected "not here" answers; anything else is a fault. */
const ABSENT_CODES = new Set(['ENOENT', 'ENOTDIR']);

/** @param {string} candidate */
function _isRegularFile(candidate) {
	try {
		return statSync(candidate).isFile();
	} catch (error) {
		if (ABSENT_CODES.has(/** @type {NodeJS.ErrnoException} */ (error).code)) return false;
		throw error;
	}
}

/**
 * EACCES from accessSync is the kernel's "not executable" answer, the expected
 * case that keeps the search going. Any other failure (I/O error, unexpected
 * permission fault on the lookup itself) is rethrown rather than hidden.
 * @param {string} candidate
 */
function _isExecutable(candidate) {
	try {
		accessSync(candidate, constants.X_OK);
		return true;
	} catch (error) {
		if (/** @type {NodeJS.ErrnoException} */ (error).code === 'EACCES') return false;
		throw error;
	}
}
