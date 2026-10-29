import assert from 'node:assert/strict';
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { resolveTool, runnable } from './resolve-tool';

const POSIX = process.platform !== 'win32';

function tree(): { root: string; bin: (dir: string, name: string, mode?: number) => string } {
	const root = mkdtempSync(path.join(tmpdir(), 'resolve-tool-'));
	return {
		root,
		bin(dir, name, mode = 0o755) {
			mkdirSync(path.join(root, dir), { recursive: true });
			const file = path.join(root, dir, name);
			writeFileSync(file, '#!/bin/sh\n');
			chmodSync(file, mode);
			return file;
		}
	};
}

test('the first absolute PATH entry holding an executable wins', { skip: !POSIX && 'POSIX execute bits' }, () => {
	const t = tree();
	try {
		const first = t.bin('a', 'uv');
		t.bin('b', 'uv');
		const env = { PATH: [path.join(t.root, 'a'), path.join(t.root, 'b')].join(':') };
		assert.equal(resolveTool('uv', env, 'linux'), first);
	} finally {
		rmSync(t.root, { recursive: true, force: true });
	}
});

test('a non-executable file and a directory are passed over for a later executable', { skip: !POSIX && 'POSIX execute bits' }, () => {
	const t = tree();
	try {
		t.bin('a', 'uv', 0o644);
		mkdirSync(path.join(t.root, 'b', 'uv'), { recursive: true });
		const real = t.bin('c', 'uv');
		const env = { PATH: ['a', 'b', 'c'].map((d) => path.join(t.root, d)).join(':') };
		assert.equal(resolveTool('uv', env, 'linux'), real);
	} finally {
		rmSync(t.root, { recursive: true, force: true });
	}
});

test('a relative PATH entry is never searched, so a tool in the working directory cannot shadow', () => {
	const seen: string[] = [];
	const env = { PATH: ['.', 'bin', '/opt/tools'].join(':') };
	const found = resolveTool('uv', env, 'linux', (file) => (seen.push(file), file === '/opt/tools/uv'));
	assert.equal(found, '/opt/tools/uv');
	assert.deepEqual(seen, ['/opt/tools/uv']);
});

test('Windows tries each PATHEXT extension, from Path, with ; as the separator', () => {
	const seen: string[] = [];
	const env = { Path: 'C:\\tools;D:\\bin', PATHEXT: '.COM;.EXE' };
	const found = resolveTool('uv', env, 'win32', (file) => (seen.push(file), file === 'D:\\bin\\uv.exe'));
	assert.equal(found, 'D:\\bin\\uv.exe');
	assert.deepEqual(seen, ['C:\\tools\\uv', 'C:\\tools\\uv.com', 'C:\\tools\\uv.exe', 'D:\\bin\\uv', 'D:\\bin\\uv.com', 'D:\\bin\\uv.exe']);
});

test('nothing found throws, naming the tool, rather than falling back to a bare name', () => {
	assert.throws(() => resolveTool('uv', { PATH: '/nowhere:/also-nowhere' }, 'linux', () => false), /uv not found on PATH \(searched 2 absolute entries\)/);
	assert.throws(() => resolveTool('uv', {}, 'linux', () => true), /searched 0 absolute entries/);
});

test('Windows without PATHEXT throws instead of inventing extensions (Sol P1 on #6830)', () => {
	for (const env of [{ Path: 'C:\\tools' }, { Path: 'C:\\tools', PATHEXT: ';' }]) {
		assert.throws(() => resolveTool('uv', env, 'win32', () => true), /PATHEXT is unset or empty/);
	}
	// Control: POSIX never reads PATHEXT.
	assert.equal(resolveTool('uv', { PATH: '/opt/tools' }, 'linux', () => true), '/opt/tools/uv');
});

test('only a missing or unrunnable candidate is a miss; any other filesystem error is thrown (Sol P1 on #6830)', { skip: !POSIX && 'POSIX execute bits' }, () => {
	const t = tree();
	try {
		assert.equal(runnable(path.join(t.root, 'absent'), 'linux'), false);
		assert.equal(runnable(path.join(t.bin('a', 'uv'), 'under-a-file'), 'linux'), false);
		assert.equal(runnable(t.bin('b', 'uv', 0o644), 'linux'), false);
		assert.equal(runnable(t.bin('c', 'uv'), 'linux'), true);
		// A path of a NUL byte fails with ERR_INVALID_ARG_VALUE, which is no answer about the file.
		assert.throws(() => runnable(path.join(t.root, 'bad\0name'), 'linux'));
	} finally {
		rmSync(t.root, { recursive: true, force: true });
	}
});
