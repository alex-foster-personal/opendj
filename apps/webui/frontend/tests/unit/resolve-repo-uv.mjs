/**
 * Resolve `uv` through the repo Python seam (apps/shared/spawn_binaries.py).
 * Node tests must not spawn a bare `uv` program name (portable-by-default).
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

function repoVenvPython(repoRoot) {
	const candidates = [
		resolve(repoRoot, '.venv/bin/python'),
		resolve(repoRoot, '.venv/Scripts/python.exe')
	];
	for (const candidate of candidates) {
		if (existsSync(candidate)) {
			return candidate;
		}
	}
	throw new Error(
		`repo venv python not found under ${repoRoot}/.venv; run uv sync in the repository root`
	);
}

let cachedUv;

/** @param {string} repoRoot absolute path to the music-dj-tools repository root */
export function resolvedRepoUv(repoRoot) {
	if (cachedUv) {
		return cachedUv;
	}
	const python = repoVenvPython(repoRoot);
	const result = spawnSync(python, ['-m', 'apps.shared.spawn_binaries', 'uv'], {
		cwd: repoRoot,
		encoding: 'utf8'
	});
	if (result.status !== 0) {
		const detail = (result.stderr || result.stdout || '').trim();
		throw new Error(detail || `spawn_binaries uv exited ${result.status}`);
	}
	const uv = result.stdout.trim();
	if (!uv) {
		throw new Error('spawn_binaries uv returned an empty path');
	}
	cachedUv = uv;
	return uv;
}
