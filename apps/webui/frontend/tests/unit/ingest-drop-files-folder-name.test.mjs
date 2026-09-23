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

// [if] a multi-disc folder holds two files with one basename [then] the walker keeps their folder-relative paths distinct [else stop].
function makeFileEntry(name, fullPath) {
	return {
		isFile: true,
		isDirectory: false,
		name,
		fullPath,
		file(cb) {
			cb(new File(['x'], name, { type: 'audio/mpeg' }));
		}
	};
}

function makeTree(name, fullPath, children) {
	let sent = false;
	return {
		isFile: false,
		isDirectory: true,
		name,
		fullPath,
		createReader() {
			return {
				readEntries(cb) {
					cb(sent ? [] : children);
					sent = true;
				}
			};
		}
	};
}

describe('collectDroppedAudioFiles on a multi-disc folder', () => {
	it('uploads Disc 1/01.mp3 and Disc 2/01.mp3 under distinct relative names', async () => {
		const album = makeTree('Album', '/Album', [
			makeTree('Disc 1', '/Album/Disc 1', [makeFileEntry('01.mp3', '/Album/Disc 1/01.mp3')]),
			makeTree('Disc 2', '/Album/Disc 2', [makeFileEntry('01.mp3', '/Album/Disc 2/01.mp3')])
		]);
		const dt = { items: [{ webkitGetAsEntry: () => album }], files: [] };
		const files = await drop.collectDroppedAudioFiles(dt);
		assert.deepEqual(
			files.map((f) => f.name),
			['Album/Disc 1/01.mp3', 'Album/Disc 2/01.mp3']
		);
		assert.equal(drop.collectDroppedFolderName(dt), 'Album');
	});
});
