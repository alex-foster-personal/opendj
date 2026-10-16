// Codex P2 on #6186 (r4266059818): fixture servers must not spawn a bare
// `uv`, which leaves the lookup to whatever PATH the pnpm runner inherited.
// resolveUv() returns an absolute path: MDT_UV_BIN when set (it must exist),
// else the first `uv` (with each PATHEXT on Windows) on PATH, else uv's own
// default install dirs. It throws a clear error instead of an ENOENT spawn.
import { accessSync, constants, statSync } from 'node:fs';
import { homedir } from 'node:os';
import { delimiter, isAbsolute, join, resolve } from 'node:path';

function runnable(path) {
	try {
		if (!statSync(path).isFile()) return false;
		if (process.platform !== 'win32') accessSync(path, constants.X_OK);
		return true;
	} catch {
		return false;
	}
}

export function resolveUv(env = process.env, platform = process.platform) {
	const override = env.MDT_UV_BIN;
	if (override) {
		const path = isAbsolute(override) ? override : resolve(override);
		if (!runnable(path)) throw new Error(`MDT_UV_BIN=${override} is not an executable file`);
		return path;
	}
	const exts = platform === 'win32' ? (env.PATHEXT || '.EXE;.CMD;.BAT').split(';').filter(Boolean) : [''];
	const dirs = (env.PATH || env.Path || '').split(delimiter).filter(Boolean);
	// uv's installer defaults, for runners whose PATH leaves them out.
	dirs.push(join(homedir(), '.local', 'bin'), join(homedir(), '.cargo', 'bin'));
	for (const dir of dirs) {
		for (const ext of exts) {
			// Codex P2 on #6186 (r4266167702): a relative PATH entry is resolved
			// against this process's cwd now, so the spawn (with its own cwd)
			// gets the same absolute file that was checked.
			const path = resolve(dir, `uv${ext}`);
			if (runnable(path)) return path;
		}
	}
	throw new Error('uv not found on PATH or in ~/.local/bin or ~/.cargo/bin; set MDT_UV_BIN');
}
