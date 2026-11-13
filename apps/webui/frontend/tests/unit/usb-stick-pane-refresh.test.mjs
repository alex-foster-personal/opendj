// requirement: USBPLAY-14
// requirement: USBPLAY-13990
/**
 * Hermetic helpers for tests/e2e/usb-stick-pane-refresh.spec.ts and the locked
 * library wire the stick pane parses.
 *
 * - [if] the e2e initial export lists three titles [then] parseUsbLibraryWire accepts it, [else stop]
 * - [if] the e2e replacement export lists four titles [then] its ids differ from the initial three, [else stop]
 * - [if] the not-mounted refusal carries USB_STICK_NOT_MOUNTED [then] the payload matches the daemon shape, [else stop]
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FIXTURE = await loadTypeScriptModule(
	'tests/e2e/support/usb-stick-pane-refresh-fixture.ts',
	{}
);

const { parseUsbLibraryWire } = await loadTypeScriptModule('src/lib/rb/usb-row-wire.ts', {});

test('[if] a stick pane is open with an initial export response [then] the wire parser accepts the e2e initial library', () => {
	const parsed = parseUsbLibraryWire(FIXTURE.USB_STICK_LIBRARY_INITIAL);
	assert.equal(parsed.tracks.length, 3);
	assert.deepEqual(
		parsed.tracks.map((t) => t.title),
		['Initial Alpha', 'Initial Beta', 'Initial Gamma']
	);
});

test('[if] the next refresh response has changed rows [then] the replacement export is not the initial set', () => {
	const initial = new Set(FIXTURE.USB_STICK_LIBRARY_INITIAL.tracks.map((t) => t.id));
	const replaced = FIXTURE.USB_STICK_LIBRARY_REPLACED.tracks.map((t) => t.id);
	assert.equal(replaced.length, 4);
	assert.ok(
		replaced.some((id) => !initial.has(id)),
		'a refresh must be able to show rows the first read did not'
	);
	const parsed = parseUsbLibraryWire(FIXTURE.USB_STICK_LIBRARY_REPLACED);
	assert.equal(parsed.counts.tracks, 4);
});

test('[if] the refresh response fails [then] the not-mounted detail matches the daemon refusal shape', () => {
	const detail = FIXTURE.usbStickNotMountedDetail();
	assert.equal(detail.detail.code, 'USB_STICK_NOT_MOUNTED');
	assert.match(String(detail.detail.message), /mounted stick/);
});

test('the browser spec names all three USBPLAY-14 acceptance lines', () => {
	const spec = readFileSync(
		fileURLToPath(new URL('../e2e/usb-stick-pane-refresh.spec.ts', import.meta.url)),
		'utf8'
	);
	for (const needle of [
		'[if] a stick pane is open with an initial export response',
		'[if] the next refresh response has changed rows',
		'[if] the refresh response fails'
	]) {
		assert.match(spec, new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
	}
});
