/**
 * Endpoints and throwaway data dir for the library-jobs ordering e2e (#1975).
 *
 * Does not claim worktree ports. Builds a 2-track fixture via deckload_fixture
 * (stdlib wave, no ffmpeg). The daemon drains user lanes with a dry runner so
 * no Modal GPU is required.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const LIBRARY_JOBS_E2E_BACKEND_PORT = 8704;
export const LIBRARY_JOBS_E2E_FRONTEND_PORT = 5277;

const RESERVED = new Set([
	4455, 4456, 5214, 5216, 5273, 5311, 5320, 5321, 5322, 5323, 5324, 5326, 5399, 5173,
	8585, 8682, 8685, 8686, 8688, 8690, 8691, 8692, 8695, 8696, 8697, 8698, 8699, 8703,
	9402, 9405, 9408, 9414, 9473
]);

export interface LibraryJobsE2eEndpoints {
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
			`${name}=${value} is a port another lane or suite owns; pick another`
		);
	}
	return value;
}

export function resolveEndpoints(): LibraryJobsE2eEndpoints {
	const backendPort = requirePort(
		'LIBRARY_JOBS_E2E_BACKEND_PORT',
		LIBRARY_JOBS_E2E_BACKEND_PORT
	);
	const frontendPort = requirePort(
		'LIBRARY_JOBS_E2E_FRONTEND_PORT',
		LIBRARY_JOBS_E2E_FRONTEND_PORT
	);
	if (backendPort === frontendPort) {
		throw new Error('the library-jobs e2e backend and frontend ports must differ');
	}
	return {
		backendPort,
		frontendPort,
		backendOrigin: `http://127.0.0.1:${backendPort}`,
		frontendOrigin: `http://127.0.0.1:${frontendPort}`
	};
}

export function seedDataDir(repositoryRoot: string): string {
	const existing = process.env.LIBRARY_JOBS_E2E_DATA_DIR;
	if (existing !== undefined && existing !== '') return existing;

	const root = mkdtempSync(join(tmpdir(), 'library-jobs-e2e-'));
	const dataDir = join(root, 'data');
	mkdirSync(dataDir, { recursive: true });
	execFileSync(
		'uv',
		[
			'run',
			'--no-sync',
			'python',
			'-m',
			'apps.webui.frontend.tests.e2e.support.deckload_fixture',
			'--data-dir',
			dataDir
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
