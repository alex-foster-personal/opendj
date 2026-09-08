import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const table = readFileSync(`${SRC}/lib/components/rb/browser/TrackTable.svelte`, 'utf8');

test('context-menu selection ignores pointer modifiers and selects keyboard targets', () => {
	const pointer = table.match(/function openTrackMenu[\s\S]*?\n\t}\n\n\tfunction onTrackKeydown/);
	assert.ok(pointer, 'track context-menu pointer handler must exist');
	assert.match(pointer[0], /onselectrow\(row\);/, 'context-click must not extend selection from Control or Command');

	const keyboard = table.match(/function onTrackKeydown[\s\S]*?\n\t}\n\n\tfunction onColResizeStart/);
	assert.ok(keyboard, 'track context-menu keyboard handler must exist');
	assert.match(keyboard[0], /onselectrow\(row\);/, 'keyboard context menu must select its focused row');
});
