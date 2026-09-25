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
