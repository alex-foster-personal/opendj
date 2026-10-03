import assert from 'node:assert/strict';
import test from 'node:test';

import {
	formatWatcherFolderLines,
	parseWatcherFolderLines,
	validateWatcherFolderPathSyntax
} from '../../src/lib/rb/library-watcher-folders.ts';

test('parseWatcherFolderLines trims, dedupes, and rejects relative paths', () => {
	const paths = parseWatcherFolderLines('/Users/dev/Music\n\n/Users/dev/Music\n/tmp/watch\n');
	assert.deepEqual(paths, ['/Users/dev/Music', '/tmp/watch']);
	assert.throws(
		() => parseWatcherFolderLines('relative/path'),
		/absolute path/
	);
	assert.throws(
		() => validateWatcherFolderPathSyntax('/bad/../path'),
		/\.\./
	);
});

test('formatWatcherFolderLines round-trips parse', () => {
	const text = formatWatcherFolderLines(['/a', '/b']);
	assert.equal(text, '/a\n/b');
	assert.deepEqual(parseWatcherFolderLines(text), ['/a', '/b']);
});

// PR #4014 (Sol P2): native Windows absolute paths are accepted alongside
// POSIX ones, while relative and drive-relative forms still fail.
test('Windows drive-letter and UNC absolute paths are accepted', () => {
	for (const path of ['C:\\Music', 'C:/Music', 'd:\\DJ\\Crates', '\\\\server\\share', '\\\\nas\\music\\house']) {
		assert.doesNotThrow(() => validateWatcherFolderPathSyntax(path), path);
	}
	assert.deepEqual(parseWatcherFolderLines('C:\\Music\r\n\\\\server\\share\n/Users/dev/Music'), [
		'C:\\Music',
		'\\\\server\\share',
		'/Users/dev/Music'
	]);
});

test('relative, drive-relative and share-less forms are still rejected', () => {
	for (const path of ['Music', 'C:Music', '\\Music', '\\\\server', '\\\\server\\', 'relative\\dir']) {
		assert.throws(() => validateWatcherFolderPathSyntax(path), /absolute path/, path);
	}
	assert.throws(() => validateWatcherFolderPathSyntax('C:\\a\\..\\b'), /\.\./);
	assert.throws(() => validateWatcherFolderPathSyntax('/Music/..'), /\.\./);
	// Codex P2 on PR #4014: two dots inside a folder name are not parent traversal.
	assert.doesNotThrow(() => validateWatcherFolderPathSyntax('/Music/AC..DC'));
	assert.doesNotThrow(() => validateWatcherFolderPathSyntax('D:\\Music\\AC..DC'));
});
