import assert from 'node:assert/strict';
import { describe, it, before } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cells;

before(async () => {
	cells = await loadTypeScriptModule('src/lib/rb/file-path-cells.ts');
});

describe('file-path-cells', () => {
	it('splits posix and Windows paths into file name and parent folder', () => {
		assert.deepEqual(cells.splitLocalFilePath('/music/sets/track.mp3'), {
			fileName: 'track.mp3',
			location: '/music/sets'
		});
		assert.deepEqual(cells.splitLocalFilePath('C:\\DJ Library\\track.mp3'), {
			fileName: 'track.mp3',
			location: 'C:\\DJ Library'
		});
	});

	it('shows streaming provider in Location and blank File name', () => {
		const row = {
			file_path: 'tidal:track:99560085',
			is_streaming: true,
			file_availability: 'tidal-streaming'
		};
		assert.deepEqual(cells.filePathCellsFor(row), { fileName: '', location: 'Tidal' });
	});

	it('sorts file name and location case-insensitively', () => {
		const low = { file_path: '/a/Beta.mp3' };
		const high = { file_path: '/a/alpha.mp3' };
		assert.equal(cells.fileNameSortValue(low), 'beta.mp3');
		assert.equal(cells.fileNameSortValue(high), 'alpha.mp3');
		assert.ok(cells.fileNameSortValue(low).localeCompare(cells.fileNameSortValue(high)) > 0);
	});
});
