import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chromiumSandboxArgs } from './chromium-sandbox';
import { runningAsPosixRoot } from './platform-identity';

test('a root run on POSIX turns the Chromium sandbox off', () => {
	assert.deepEqual(chromiumSandboxArgs(runningAsPosixRoot('linux', 0)), ['--no-sandbox']);
	assert.deepEqual(chromiumSandboxArgs(runningAsPosixRoot('darwin', 0)), ['--no-sandbox']);
});

test('a non-root POSIX run keeps the sandbox', () => {
	assert.deepEqual(chromiumSandboxArgs(runningAsPosixRoot('linux', 1000)), []);
});

test('Windows never turns the sandbox off, whatever uid is passed', () => {
	assert.deepEqual(chromiumSandboxArgs(runningAsPosixRoot('win32', null)), []);
	assert.deepEqual(chromiumSandboxArgs(runningAsPosixRoot('win32', 0)), []);
});
