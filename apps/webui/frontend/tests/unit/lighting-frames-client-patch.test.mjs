// requirement: LIGHT-14090
// [if] parsePatch gets the daemon venue store {version, active, patches} [then] it returns the active patch's fixtures, [else stop].
// [if] parsePatch gets the old stage shape {version, name, fixtures} [then] it still returns those fixtures, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/lighting/frames-client.ts');
});

test('[if] parsePatch gets the daemon venue store [then] it resolves the active patch fixtures, [else stop].', () => {
	const store = {
		version: 1,
		active: 'club',
		patches: [
			{ name: 'bar', fixtures: [{ id: 'bar-1', groups: [], position: { x: 0.1, y: 0.1 } }] },
			{ name: 'club', fixtures: [{ id: 'par-1', groups: ['left'], position: { x: 0.25, y: 0.5 } }] }
		]
	};
	const patch = mod.parsePatch(store);
	assert.equal(patch.version, 1);
	assert.deepEqual(patch.fixtures, [{ id: 'par-1', x: 0.25, y: 0.5, groups: ['left'] }]);
	assert.throws(() => mod.parsePatch({ ...store, active: 'missing' }), /patch\.active/);
});

test('[if] parsePatch gets the old {version, name, fixtures} shape [then] it still returns them, [else stop].', () => {
	const patch = mod.parsePatch({
		version: 'abc',
		name: 'club',
		fixtures: [{ id: 'par-1', x: 0.25, y: 0.5, groups: [] }]
	});
	assert.equal(patch.version, 'abc');
	assert.deepEqual(patch.fixtures, [{ id: 'par-1', x: 0.25, y: 0.5, groups: [] }]);
});
