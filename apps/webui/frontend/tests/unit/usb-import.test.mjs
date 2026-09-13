// requirement: LIBMX-13
// [if] a detected USB drive is clicked [then] an import action is reachable from that row, not just a yours/not-yours classifier, [else stop].
// [if] a folder containing audio files is dropped onto the ingest surface [then] the files inside it are found and staged, not reported as no audio files, [else stop].
// [if] a batch is staged and awaiting a manual Rekordbox import [then] a persistent UI indicator says so until the user confirms it is done, [else stop].
// [if] a possible-dup match is shown during staging [then] the user can accept or reject it inline, not just read a status line, [else stop].

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

describe('usb import contracts (LIBMX-13)', () => {
	it('UsbSourceList has import button and uses importUsbVolume', () => {
		const src = readFileSync(
			new URL('../../src/lib/components/rb/browser/UsbSourceList.svelte', import.meta.url),
			'utf8'
		);
		assert.match(src, /data-testid="usb-source-import"/);
		assert.match(src, /importUsbVolume/);
	});

	it('LibraryNav mounts UsbPanel and starts USB watch', () => {
		const src = readFileSync(
			new URL('../../src/lib/components/rb/browser/LibraryNav.svelte', import.meta.url),
			'utf8'
		);
		assert.match(src, /import UsbPanel/);
		assert.match(src, /startUsbWatch/);
		assert.match(src, /stopUsbWatch/);
	});

	it('usb-import calls startFolderImport', () => {
		const src = readFileSync(
			new URL('../../src/lib/rb/usb-import.ts', import.meta.url),
			'utf8'
		);
		assert.match(src, /startFolderImport/);
	});

	it('usb-tracker still has no api.POST', () => {
		const tracker = readFileSync(
			new URL('../../src/lib/rb/usb-tracker.svelte.ts', import.meta.url),
			'utf8'
		);
		assert.doesNotMatch(tracker, /api\.POST/);
	});
});
