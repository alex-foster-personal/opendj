import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runningAsPosixRoot } from './platform-identity';

test('uid 0 on Linux or macOS is root', () => {
	assert.equal(runningAsPosixRoot('linux', 0), true);
	assert.equal(runningAsPosixRoot('darwin', 0), true);
});

test('any other POSIX uid, or none, is not root', () => {
	assert.equal(runningAsPosixRoot('linux', 1000), false);
	assert.equal(runningAsPosixRoot('linux', null), false);
});

test('Windows is never root here, whatever uid is passed', () => {
	assert.equal(runningAsPosixRoot('win32', null), false);
	assert.equal(runningAsPosixRoot('win32', 0), false);
});
