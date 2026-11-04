// The engine's data directory, per OS (ADR windows-data-dir-and-app-identity).
//
// Pure: every input is an argument, so tests run it under a real environment in
// a child process and under an explicit platform in-process. The Python twin is
// apps/shared/engine_data_dir.py; tests/shared/test_engine_data_dir.py asserts
// the two resolve the same path.
//
//   macOS    <appData>/<identifier>            (Tauri's app_data_dir; unchanged)
//   Linux    $XDG_DATA_HOME/<identifier>, else ~/.local/share/<identifier> (unchanged)
//   Windows  %LOCALAPPDATA%\<identifier>       (never Roaming; unset is an error)

import * as path from 'node:path';

export class DataDirError extends Error {}

export interface DataDirInputs {
	readonly platform: NodeJS.Platform;
	readonly env: Readonly<Record<string, string | undefined>>;
	readonly homedir: string;
	/** Electron's `app.getPath('appData')`; used on macOS only. */
	readonly appDataDir: string;
}

export function resolveEngineDataDir(identifier: string, inputs: DataDirInputs): string {
	if (inputs.platform === 'win32') {
		const localAppData = inputs.env.LOCALAPPDATA?.trim();
		if (localAppData === undefined || localAppData === '') {
			throw new DataDirError('LOCALAPPDATA is unset; the Windows engine data dir lives under %LOCALAPPDATA% and has no fallback.');
		}
		if (!path.win32.isAbsolute(localAppData)) {
			throw new DataDirError(`LOCALAPPDATA must be an absolute path, got ${JSON.stringify(localAppData)}.`);
		}
		return path.win32.join(localAppData, identifier);
	}
	if (inputs.platform === 'linux') {
		const base = inputs.env.XDG_DATA_HOME?.trim() || path.posix.join(inputs.homedir, '.local', 'share');
		return path.posix.join(base, identifier);
	}
	return path.posix.join(inputs.appDataDir, identifier);
}
