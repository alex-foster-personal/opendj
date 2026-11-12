/**
 * @pytest.mark.requirement LIBM-12633
 * [if] Cmd+A over the track list [then] grid-jump does not steal it, [else stop].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let routing;
let targetMod;

before(async () => {
	routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
	targetMod = await loadTypeScriptModule('src/lib/rb/library-track-list-target.ts');
});

const title = { closest: (sel) => (sel.includes('title-text') ? {} : null) };
const page = { closest: () => null };

test('title-text is a library track list target', () => {
	assert.equal(targetMod.isLibraryTrackListTarget(title), true);
	assert.equal(targetMod.isLibraryTrackListTarget(page), false);
});

test('Cmd+A stays select-all in the library even after a deck press', () => {
	const ev = {
		code: '',
		key: 'a',
		target: title,
		metaKey: true,
		ctrlKey: false,
		altKey: false,
		shiftKey: false
	};
	assert.equal(
		routing.resolvePerformanceShortcutAction(ev, { gridJump: true, deckFocus: true }),
		null
	);
});
