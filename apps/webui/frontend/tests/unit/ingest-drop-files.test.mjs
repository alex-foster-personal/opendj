// requirement: LIBMX-13
// [if] a detected USB drive is clicked [then] an import action is reachable from that row, not just a yours/not-yours classifier, [else stop].
// [if] a folder containing audio files is dropped onto the ingest surface [then] the files inside it are found and staged, not reported as no audio files, [else stop].
// [if] a batch is staged and awaiting a manual Rekordbox import [then] a persistent UI indicator says so until the user confirms it is done, [else stop].
// [if] a possible-dup match is shown during staging [then] the user can accept or reject it inline, not just read a status line, [else stop].

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let drop;

before(async () => {
	drop = await loadTypeScriptModule('src/lib/rb/ingest-drop-files.ts');
});

function makeFileEntry(name, fullPath, content = 'audio') {
	return {
		isFile: true,
		isDirectory: false,
		name,
		fullPath,
		file(cb) {
			const f = new File([content], name, { type: 'audio/mpeg' });
			cb(f);
		}
	};
}

function makeDirEntry(name, fullPath, children) {
	return {
		isFile: false,
		isDirectory: true,
		name,
		fullPath,
		createReader() {
			let sent = false;
			return {
				readEntries(cb) {
					if (sent) {
						cb([]);
						return;
					}
					sent = true;
					cb(children);
				}
			};
		}
	};
}

describe('collectDroppedAudioFiles', () => {
	it('walks nested folders and keeps audio only', async () => {
		const mp3 = makeFileEntry('a.mp3', '/folder/a.mp3');
		const txt = makeFileEntry('readme.txt', '/folder/readme.txt');
		const dir = makeDirEntry('folder', '/folder', [mp3, txt]);
		const dt = {
			items: [{ webkitGetAsEntry: () => dir }],
			files: []
		};
		const files = await drop.collectDroppedAudioFiles(dt);
		assert.equal(files.length, 1);
		assert.equal(files[0].name, 'folder/a.mp3');
	});

	it('includes .alac extension', async () => {
		const alac = makeFileEntry('track.alac', '/track.alac');
		const dt = {
			items: [{ webkitGetAsEntry: () => alac }],
			files: []
		};
		const files = await drop.collectDroppedAudioFiles(dt);
		assert.equal(files.length, 1);
	});

	it('returns empty for empty directory', async () => {
		const dir = makeDirEntry('empty', '/empty', []);
		const dt = {
			items: [{ webkitGetAsEntry: () => dir }],
			files: []
		};
		const files = await drop.collectDroppedAudioFiles(dt);
		assert.deepEqual(files, []);
	});

	it('falls back to flat FileList when no webkit entries', async () => {
		const f = new File(['x'], 'flat.mp3', { type: 'audio/mpeg' });
		const dt = { items: [], files: [f] };
		const files = await drop.collectDroppedAudioFiles(dt);
		assert.equal(files.length, 1);
		assert.equal(files[0].name, 'flat.mp3');
	});
});
