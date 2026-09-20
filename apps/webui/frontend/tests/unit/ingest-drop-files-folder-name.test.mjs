// requirement: LIBUX-16
// [if] collectDroppedFolderName sees a directory entry [then] it returns that name [else stop].

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let drop;

before(async () => {
	drop = await loadTypeScriptModule('src/lib/rb/ingest-drop-files.ts');
});

function makeDirEntry(name) {
	return {
		isFile: false,
		isDirectory: true,
		name,
		fullPath: `/${name}`,
		createReader() {
			return { readEntries(cb) { cb([]); } };
		}
	};
}

describe('collectDroppedFolderName', () => {
	it('returns the first top-level directory name', () => {
		const dt = {
			items: [{ webkitGetAsEntry: () => makeDirEntry('Agnes Obel') }]
		};
		assert.equal(drop.collectDroppedFolderName(dt), 'Agnes Obel');
	});

	it('returns null for flat file drops', () => {
		const dt = {
			items: [{
				webkitGetAsEntry: () => ({
					isFile: true,
					isDirectory: false,
					name: 'track.mp3',
					fullPath: '/track.mp3',
					file(cb) { cb(new File(['x'], 'track.mp3')); }
				})
			}]
		};
		assert.equal(drop.collectDroppedFolderName(dt), null);
	});
});
