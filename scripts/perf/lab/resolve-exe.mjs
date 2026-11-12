// perf-lab: launch programs by resolved path, never a bare name (AGENTS.md
// "Portable by default"). A bare name is looked up on PATH by the OS at every
// launch, so a shim or another installation earlier on PATH, or a PATH that
// changed since, would run instead; and what the program prints may become a
// capture's identity. resolveExecutable finds the program once, on the PATH
// it is given, to the real file behind it (links followed); pinnedExecutable
// keeps the first resolution of a name for the life of this process, so a
// later PATH change cannot swap the program a capture reads its identity from.
import fs from 'node:fs';
import path from 'node:path';

/** The real, absolute path of the first `name` on env.PATH that is an
 * executable file, or null. Relative PATH entries are skipped (they name
 * another file per working directory). On Windows (platform 'win32') each
 * PATHEXT extension is tried, as the shell does. */
export function resolveExecutable(name, env = process.env, platform = process.platform) {
	const win = platform === 'win32';
	const key = Object.keys(env).find((k) => k.toUpperCase() === 'PATH');
	const dirs = String((key && env[key]) ?? '')
		.split(win ? ';' : ':')
		.filter((d) => d && (win ? path.win32.isAbsolute(d) : path.posix.isAbsolute(d)));
	const extKey = Object.keys(env).find((k) => k.toUpperCase() === 'PATHEXT');
	const exts = win ? ['', ...String((extKey && env[extKey]) || '.EXE;.CMD;.BAT;.COM').split(';').filter(Boolean)] : [''];
	for (const dir of dirs) {
		for (const ext of exts) {
			const candidate = path.join(dir, name + ext);
			try {
				if (!fs.statSync(candidate).isFile()) continue;
				if (!win) fs.accessSync(candidate, fs.constants.X_OK);
				return fs.realpathSync(candidate);
			} catch {
				// absent or not executable here: the next entry
			}
		}
	}
	return null;
}

const PINNED = new Map();

/** resolveExecutable, pinned: the first successful resolution of `name` in
 * this process is returned for every later call, whatever PATH then says. */
export function pinnedExecutable(name, env = process.env, platform = process.platform) {
	if (!PINNED.has(name)) {
		const found = resolveExecutable(name, env, platform);
		if (found === null) return null;
		PINNED.set(name, found);
	}
	return PINNED.get(name);
}
