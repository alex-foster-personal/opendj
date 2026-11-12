/**
 * Shared uv launch for frontend unit-test Python fixture servers (issue #13111).
 *
 * When .venv exists: --no-sync reuses the CI-provisioned venv without exact-syncing
 * back to uv.lock; a syncing `uv run` uninstalls packages while --test-concurrency=4
 * siblings import from the same `.venv` (quit-gate's engine once failed: No module
 * named '_cffi_backend'). --python <venv interpreter> stops uv from rebuilding that venv
 * when the runner patch release differs from .python-version (the server dies importing
 * pytest or uvicorn).
 * Cold checkout: --locked --extra dev so uvicorn and pytest exist before READY;
 * --locked never rewrites uv.lock. CI provisions the venv with scripts/ci_venv.sh;
 * locally use uv sync --locked --extra dev.
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

import { resolveUv } from '../tests/unit/fixtures/resolve-uv.mjs';

/**
 * @param {string} repoRoot
 * @returns {string | null}
 */
export function repoVenvPython(repoRoot) {
	const rel = process.platform === 'win32' ? join('Scripts', 'python.exe') : join('bin', 'python');
	const candidate = join(repoRoot, '.venv', rel);
	return existsSync(candidate) ? candidate : null;
}

/**
 * @param {object} options
 * @param {string} options.repoRoot
 * @param {string} options.pythonTarget script path or `-m` module name
 * @param {boolean} [options.pythonTargetIsModule]
 * @param {readonly string[]} [options.extraArgs]
 * @param {NodeJS.ProcessEnv} [options.env]
 * @returns {{ command: string, args: string[], env: NodeJS.ProcessEnv, cwd: string }}
 */
export function fixtureServerLaunch({
	repoRoot,
	pythonTarget,
	pythonTargetIsModule = false,
	extraArgs = [],
	env = {}
}) {
	const venvPython = repoVenvPython(repoRoot);
	const uvPrefix = venvPython
		? ['run', '--no-sync', '--python', venvPython]
		: ['run', '--locked', '--extra', 'dev'];
	const pythonTail = pythonTargetIsModule
		? ['python', '-m', pythonTarget, ...extraArgs]
		: ['python', pythonTarget, ...extraArgs];
	return {
		command: resolveUv(),
		args: [...uvPrefix, ...pythonTail],
		cwd: repoRoot,
		env: {
			...process.env,
			PYTHONPATH: repoRoot,
			MDT_LIBRARY_MODE: 'local',
			...env
		}
	};
}

/**
 * Preflight: the same uv interpreter path must be able to `import uvicorn`.
 *
 * @param {{ command: string, args: string[], env: NodeJS.ProcessEnv, cwd: string }} launch
 * @returns {Promise<void>}
 */
export function assertUvicornImportable(launch) {
	const pythonIdx = launch.args.indexOf('python');
	if (pythonIdx === -1) {
		return Promise.reject(new Error('fixture launch args missing python subcommand'));
	}
	const probeArgs = [...launch.args.slice(0, pythonIdx + 1), '-c', 'import uvicorn'];
	return new Promise((resolve, reject) => {
		let stderr = '';
		const child = spawn(launch.command, probeArgs, {
			cwd: launch.cwd,
			env: launch.env,
			stdio: ['ignore', 'ignore', 'pipe']
		});
		child.stderr.on('data', (chunk) => {
			stderr += String(chunk);
		});
		child.on('error', (error) => reject(error));
		child.on('close', (code) => {
			if (code === 0) {
				resolve();
				return;
			}
			const hint = repoVenvPython(launch.cwd)
				? 'ensure CI ran scripts/ci_venv.sh or the venv includes the dev extra'
				: 'provision with uv sync --locked --extra dev at the repository root';
			const detail =
				stderr.includes('ModuleNotFoundError') || stderr.includes('uvicorn')
					? stderr.trim()
					: stderr.trim() || `exit ${code}`;
			reject(
				new Error(
					`uvicorn is not importable in the fixture-server environment (${hint}): ${detail}`
				)
			);
		});
	});
}
