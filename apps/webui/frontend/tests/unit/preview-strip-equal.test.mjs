// requirement: RESCUE-13701
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let previewStripsEqual;

before(async () => {
	({ previewStripsEqual } = await loadTypeScriptModule('src/lib/rb/api-rb.ts'));
});

test('previewStripsEqual matches identical band bytes across Uint8Array instances', () => {
	const bands = new Uint8Array([1, 2, 3]);
	const a = { cols: 120, bands, max: 10 };
	const b = { cols: 120, bands: new Uint8Array([1, 2, 3]), max: 10 };
	assert.equal(
		previewStripsEqual(a, b),
		true,
		'[if] strips match [then] equal is true, [else stop]'
	);
});

test('previewStripsEqual rejects different max', () => {
	const bands = new Uint8Array([1, 2, 3]);
	assert.equal(
		previewStripsEqual(
			{ cols: 120, bands, max: 10 },
			{ cols: 120, bands: new Uint8Array(bands), max: 11 }
		),
		false
	);
});
