/**
 * Endpoints and the throwaway data dir for the stems progress e2e.
 *
 * DELIBERATELY DOES NOT CLAIM PORTS. The claim helper writes the worktree's
 * root `.env`, and this lane must not touch that file or the port pairs other
 * lanes are already bound to. Ports come from the environment with a default
 * pair checked free at authoring time; override with STEMS_E2E_* if they are
 * taken. A bound port fails the run rather than silently attaching to whatever
 * is already there.
 *
 * DELIBERATELY DOES NOT USE A REAL LIBRARY. The savepoint gate points at the
 * primary checkout's data dir on purpose; this one must not, because it
 * ENQUEUES WORK and writes bundles. It builds its own throwaway data dir under
 * the OS temp root, so nothing it does can reach a real library, the lane data
 * dir, or an existing stem bundle.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/** Free on this machine at authoring time, and outside every reserved pair
 * (8585/5173 primary, 8685/9405 and 8682/9402 in use by other lanes). */
const DEFAULT_BACKEND_PORT = 8688;
const DEFAULT_FRONTEND_PORT = 9408;
const RESERVED = new Set([8585, 5173, 8685, 9405, 8682, 9402, 8693]);

export interface StemsE2eEndpoints {
	backendOrigin: string;
	frontendOrigin: string;
	backendPort: number;
	frontendPort: number;
}

function requirePort(name: string, fallback: number): number {
	const raw = process.env[name];
	const value = raw === undefined || raw.trim() === '' ? fallback : Number(raw);
	if (!Number.isSafeInteger(value) || value < 1024 || value > 65_535) {
		throw new Error(`${name} must be a port between 1024 and 65535, got ${raw}`);
	}
	if (RESERVED.has(value)) {
		throw new Error(
			`${name}=${value} is a port another lane or the primary checkout owns; pick another`
		);
	}
	return value;
}

export function resolveEndpoints(): StemsE2eEndpoints {
	const backendPort = requirePort('STEMS_E2E_BACKEND_PORT', DEFAULT_BACKEND_PORT);
	const frontendPort = requirePort('STEMS_E2E_FRONTEND_PORT', DEFAULT_FRONTEND_PORT);
	if (backendPort === frontendPort) {
		throw new Error('the stems e2e backend and frontend ports must differ');
	}
	return {
		backendPort,
		frontendPort,
		backendOrigin: `http://127.0.0.1:${backendPort}`,
		frontendOrigin: `http://127.0.0.1:${frontendPort}`
	};
}

/**
 * A throwaway data dir holding a handful of real tracks.
 *
 * More than one on purpose. The suite asserts a bar is ON SCREEN while work
 * runs, so the run has to last longer than the browser takes to notice it; a
 * single 3-second track finishes inside that window and the assertion becomes
 * a coin toss. A dozen gives comfortable headroom and still costs seconds.
 */
export function seedDataDir(repositoryRoot: string, tracks = 12): string {
	// Playwright evaluates the config once in the runner and again in every
	// worker. Without this, each evaluation would build its own library and
	// the worker would point at a data dir the engine was never started on.
	const existing = process.env.STEMS_E2E_DATA_DIR;
	if (existing !== undefined && existing !== '') return existing;

	const root = mkdtempSync(join(tmpdir(), 'stems-e2e-'));
	const dataDir = join(root, 'data');
	mkdirSync(dataDir, { recursive: true });
	execFileSync(
		'uv',
		[
			'run',
			'--no-sync',
			'python',
			'-m',
			'tests.stems.seed_e2e_library',
			'--data-dir',
			dataDir,
			'--tracks',
			String(tracks)
		],
		{
			cwd: repositoryRoot,
			encoding: 'utf8',
			stdio: ['ignore', 'pipe', 'inherit'],
			env: { ...process.env, MDT_LIBRARY_MODE: 'local', MDT_DATA_DIR: dataDir }
		}
	);
	return dataDir;
}
