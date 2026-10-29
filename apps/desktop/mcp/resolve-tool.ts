/**
 * Resolves a command-line tool to an absolute path once, so every launch runs the
 * same file and none relies on the child's own PATH lookup (AGENTS.md, "Launch
 * programs by resolved path"; Codex on #6830).
 */
import { accessSync, constants, statSync } from 'node:fs';
import path from 'node:path';

/**
 * The errors that mean "no usable tool here": absent, a path through a non-directory,
 * a symlink loop, or no permission to run it. Anything else (EMFILE, EIO) is a failed
 * look, not an answer, so it is thrown rather than read as a miss that lets a later
 * PATH entry win (Sol P1 on #6830).
 */
const NOT_HERE = new Set(['ENOENT', 'ENOTDIR', 'ELOOP', 'ENAMETOOLONG', 'EACCES', 'EPERM']);

/** Whether `file` is a regular file this process may execute (on Windows, any regular file). */
export function runnable(file: string, platform: NodeJS.Platform): boolean {
	try {
		if (!statSync(file).isFile()) return false;
		if (platform !== 'win32') accessSync(file, constants.X_OK);
		return true;
	} catch (error) {
		if (NOT_HERE.has((error as NodeJS.ErrnoException).code ?? '')) return false;
		throw error;
	}
}

/**
 * The absolute path of `name` on `env.PATH`, trying each `PATHEXT` extension on
 * Windows (which must be set). Relative PATH entries are skipped: they resolve against whatever the
 * working directory happens to be, which is how a shadowing executable gets picked.
 * Throws when nothing is found, naming what was searched.
 */
export function resolveTool(
	name: string,
	env: NodeJS.ProcessEnv = process.env,
	platform: NodeJS.Platform = process.platform,
	isRunnable: (file: string) => boolean = (file) => runnable(file, platform)
): string {
	const paths = platform === 'win32' ? path.win32 : path.posix;
	const searchPath = (platform === 'win32' ? (env.Path ?? env.PATH) : env.PATH) ?? '';
	const dirs = searchPath.split(paths.delimiter).filter((dir) => dir !== '' && paths.isAbsolute(dir));
	// Windows names its executable extensions in PATHEXT; without it there is no
	// declared set to try, and inventing one could run a file the caller never
	// declared executable (Sol P1 on #6830).
	const declared = (env.PATHEXT ?? '').split(';').filter(Boolean);
	if (platform === 'win32' && declared.length === 0) {
		throw new Error(`cannot resolve ${name}: PATHEXT is unset or empty, so no executable extension is declared`);
	}
	const extensions = platform === 'win32' ? ['', ...declared] : [''];
	for (const dir of dirs) {
		for (const ext of extensions) {
			const candidate = paths.join(dir, name + ext.toLowerCase());
			if (isRunnable(candidate)) return candidate;
		}
	}
	throw new Error(`${name} not found on PATH (searched ${dirs.length} absolute entr${dirs.length === 1 ? 'y' : 'ies'})`);
}
