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
		const src =
			readFileSync(new URL('../../src/lib/components/rb/browser/UsbSourceList.svelte', import.meta.url), 'utf8') +
			readFileSync(new URL('../../src/lib/components/rb/browser/UsbSourceListView.svelte', import.meta.url), 'utf8');
		assert.match(src, /data-testid="usb-source-import"/);
		assert.match(src, /importUsbVolume/);
	});

	it('LibraryNav mounts UsbPanel on first open and starts USB watch', () => {
		const src = readFileSync(
			new URL('../../src/lib/components/rb/browser/LibraryNav.svelte', import.meta.url),
			'utf8'
		);
		// Lazy: the panel renders nothing until opened, so it stays out of the
		// /performance first-paint bundle and loads when usbTracker opens it.
		assert.doesNotMatch(src, /import UsbPanel/);
		assert.match(src, /import\('\.\.\/UsbPanel\.svelte'\)/);
		assert.match(
			src,
			/loadUsbPanel\(\s*\(\) => \(usbTracker\.panelOpen = false\),\s*\(\) => \(usbTracker\.panelOpen = true\)\s*\)/
		);
		// The failure is visible with a Retry, not only in the console (Sol P1, PR #6223).
		assert.match(src, /onfail\(\);[\s\S]{0,200}pushToast\(`The USB panel did not load: \$\{why\}`, 'error'[\s\S]{0,120}label: 'Retry',\s*handler: retry/);
		// A failed chunk closes the panel so the next open retries (Sol P2, PR #6223).
		assert.match(src, /usbPanelLoad = null;\s*onfail\(\);/);
		assert.match(src, /\{#if UsbPanelView\}\s*<UsbPanelView \/>/);
		assert.match(src, /startUsbWatch/);
		assert.match(src, /stopUsbWatch/);
	});

	it('usb-import calls startFolderImport and starts analysis', () => {
		const src = readFileSync(
			new URL('../../src/lib/rb/usb-import.ts', import.meta.url),
			'utf8'
		);
		assert.match(src, /startFolderImport/);
		assert.match(src, /startAnalysisQueueDrain/);
		assert.match(src, /openUsbImportPlan/);
	});

	it('the import plan has a likely-next bar and a virtualized queue', () => {
		const src = readFileSync(
			new URL('../../src/lib/components/rb/UsbImportPlan.svelte', import.meta.url),
			'utf8'
		);
		assert.match(src, /data-testid="usb-import-next-bar"/);
		assert.match(src, /data-testid="usb-import-queue"/);
		assert.match(src, /VirtualList/);
		assert.match(src, /Only the rows in view are mounted/);
	});

	it('usb-tracker still has no api.POST', () => {
		const tracker = readFileSync(
			new URL('../../src/lib/rb/usb-tracker.svelte.ts', import.meta.url),
			'utf8'
		);
		assert.doesNotMatch(tracker, /api\.POST/);
	});
});
