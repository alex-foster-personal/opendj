/**
 * The fixture-launch resolver (AGENTS.md "Portable by default": launch by
 * resolved path). Real PATH, real files, real program.
 *
 * Regression lines:
 * - if a fixture is spawned by bare name, then a missing program fails as a
 *   spawn ENOENT before READY instead of a named error
 * - if the resolver returns the first file with the name, then a
 *   non-executable file shadows the real program
 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { ExecutableNotFoundError, resolveExecutable } from './fixtures/resolve-executable.mjs';

test('uv resolves to an absolute path that runs', () => {
	const uv = resolveExecutable('uv');
	assert.ok(path.isAbsolute(uv), uv);
	assert.match(execFileSync(uv, ['--version'], { encoding: 'utf8' }), /^uv \d/);
});

test('a program on no PATH directory fails with a named error', () => {
	const empty = mkdtempSync(path.join(tmpdir(), 'resolve-empty-'));
	try {
		assert.throws(
			() => resolveExecutable('uv', { PATH: empty, PATHEXT: process.env.PATHEXT }),
			(error) =>
				error instanceof ExecutableNotFoundError &&
				error.executable === 'uv' &&
				/uv was not found on PATH \(searched 1 directories\)/.test(error.message)
		);
	} finally {
		rmSync(empty, { recursive: true, force: true });
	}
});

test(
	'a non-executable file of that name does not shadow the real program',
	{
		skip: process.platform === 'win32' && 'Windows has no execute permission bit; PATHEXT decides'
	},
	() => {
		const shadow = mkdtempSync(path.join(tmpdir(), 'resolve-shadow-'));
		try {
			const fake = path.join(shadow, 'uv');
			writeFileSync(fake, 'not a program\n', { encoding: 'utf8' });
			chmodSync(fake, 0o644);
			const real = resolveExecutable('uv');
			const env = { PATH: [shadow, path.dirname(real)].join(':') };
			assert.equal(resolveExecutable('uv', env), real);
		} finally {
			rmSync(shadow, { recursive: true, force: true });
		}
	}
);
