import { accessSync, constants, statSync } from 'node:fs';
import { delimiter, join } from 'node:path';

/**
 * The absolute path of a program found on PATH, so a gate launches the program it resolved and
 * not whatever a bare name finds later (the repository's resolved-executable rule). Windows
 * tries each PATHEXT suffix. Throws naming the program when it is not on PATH.
 */
export function resolveProgram(name: string): string {
	const suffixes = process.platform === 'win32' ? (process.env.PATHEXT ?? '.EXE;.CMD;.BAT;.COM').split(';') : [''];
	for (const dir of (process.env.PATH ?? '').split(delimiter)) {
		if (dir === '') continue;
		for (const suffix of suffixes) {
			const candidate = join(dir, name + suffix);
			try {
				accessSync(candidate, constants.X_OK);
				if (statSync(candidate).isFile()) return candidate;
			} catch {
				// Not here: the next directory or suffix.
			}
		}
	}
	throw new Error(`${name} is not on PATH; the Cmd K e2e gate cannot run without it`);
}
