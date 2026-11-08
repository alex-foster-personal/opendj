import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const menu = readFileSync(`${SRC}/lib/components/rb/browser/TrackContextMenu.svelte`, 'utf8');
const table = readFileSync(`${SRC}/lib/components/rb/browser/TrackTable.svelte`, 'utf8');

function readFunctionBody(source, name) {
	const start = source.indexOf(`function ${name}(`);
	assert.ok(start >= 0, `${name} must exist`);
	const open = source.indexOf('{', start);
	let depth = 0;
	for (let i = open; i < source.length; i++) {
		if (source[i] === '{') depth += 1;
		else if (source[i] === '}') {
			depth -= 1;
			if (depth === 0) return source.slice(start, i + 1);
		}
	}
	throw new Error(`${name} body not found`);
}

test('track menu binds Track info and rating runners (#9451)', () => {
	assert.match(menu, /'track-info':/);
	assert.match(menu, /`rate-\$\{n\}`/);
	assert.match(menu, /runSetRating\(targetIds, n\)/);
});

// PRRT_kwDOSEvNd860wEnM: Track info in Performance mode must use the shared dispatcher.
test('track menu dispatches track_info_open through performance IPC (PRRT_kwDOSEvNd860wEnM)', () => {
	assert.match(
		menu,
		/runPerformanceCommandFromUi\(\{\s*type: 'track_info_open'/,
		'Track info must go through the shared dispatcher so the preset lifecycle lock applies (PRRT_kwDOSEvNd860wEnM)'
	);
	assert.doesNotMatch(
		menu,
		/openTrackInfoForRow\(/,
		'Track info must not open the overlay directly (PRRT_kwDOSEvNd860wEnM)'
	);
	assert.match(
		menu,
		/import \{[^}]*runPerformanceCommandFromUi[^}]*\} from '\$lib\/rb\/performance-ipc\.svelte'/,
		'TrackContextMenu must import runPerformanceCommandFromUi (PRRT_kwDOSEvNd860wEnM)'
	);
});

test('context-menu selection ignores pointer modifiers and selects keyboard targets', () => {
	const pointer = readFunctionBody(menu, 'openMenu');
	assert.match(
		pointer,
		/onselectrow\(row\);/,
		'context-click must not extend selection from Control or Command'
	);

	const keyboard = readFunctionBody(menu, 'openFromKeyboard');
	assert.match(keyboard, /openMenu\(/, 'keyboard context menu must reuse the same open path');
	assert.match(table, /trackContextMenu\?\.openFromKeyboard\(event, row\)/);
});
