import assert from 'node:assert/strict';
import { join } from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// Issue #12581: duration_ms lives on the shared Track contract, not a BrowserPanel-only cast.
const FRONTEND_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const read = (rel) => readFileSync(join(FRONTEND_ROOT, rel), 'utf8');

test('Track is the generated TrackOut schema', () => {
	// The client Track type must alias TrackOut so it inherits every backend field.
	assert.match(
		read('src/lib/api.ts'),
		/export type Track = components\['schemas'\]\['TrackOut'\];/
	);
});

test('generated TrackOut carries duration_ms', () => {
	// A hydrated track from /tracks exposes duration_ms through the typed contract.
	const types = read('src/lib/api-types.ts');
	const start = types.indexOf('        TrackOut: {');
	assert.ok(start >= 0, 'expected TrackOut schema in api-types.ts');
	const end = types.indexOf('\n        };', start);
	assert.match(types.slice(start, end), /\n\s+duration_ms\?: number \| null;/);
});

test('BrowserPanel has no local duration_ms type extension', () => {
	// The legacy `Track & { duration_ms?: ... }` wire cast must not come back.
	const panel = read('src/lib/components/rb/BrowserPanel.svelte');
	assert.doesNotMatch(panel, /&\s*\{\s*duration_ms\??:/);
	assert.doesNotMatch(panel, /type TrackWire\b/);
});
