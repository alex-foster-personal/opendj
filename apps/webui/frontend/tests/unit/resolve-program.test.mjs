import assert from 'node:assert/strict';
import { isAbsolute } from 'node:path';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * The e2e gates launch uv by the path resolveProgram found, not by a bare name (Codex P1, #6689).
 *
 * Regression lines:
 * - if a missing program resolves to a bare name instead of refusing then broken
 * - if a found program comes back relative instead of absolute then broken
 */

let resolve;

before(async () => {
	({ resolveProgram: resolve } = await loadTypeScriptModule('tests/e2e/support/resolve-program.ts'));
});

test('a program on PATH resolves to its absolute path', { skip: process.platform === 'win32' && 'the POSIX lookup is covered by the shell tests' }, () => {
	const found = resolve('sh');
	assert.ok(isAbsolute(found), `sh resolved to ${found}`);
});

test('a program not on PATH is refused with its name, not passed on as a bare name', () => {
	assert.throws(() => resolve('no-such-program-for-the-cmdk-gate'), /no-such-program-for-the-cmdk-gate is not on PATH/);
});
