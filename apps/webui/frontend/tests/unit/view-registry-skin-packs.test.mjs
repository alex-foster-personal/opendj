// requirement: SKIN-14055 (issue #13877, PR #14055, epic #9365, epic #10175)
// [if] a UI_SKIN_CHOICES id has no SKIN_PACK_REGISTRY entry [then] the test fails naming the id, [else stop].
// [if] a pack entry's layout differs from SKIN_LAYOUT[id] [then] the test fails naming the id and both values, [else stop].
// [if] parseSkinPackId('green-room') throws [then] the test fails, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let R;
let L;
let S;
before(async () => {
	R = await loadTypeScriptModule('src/lib/rb/view-registry.ts');
	L = await loadTypeScriptModule('src/lib/rb/skin-layout.ts');
	S = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
});

test('if a UI_SKIN_CHOICES id has no pack entry, then the test names it', () => {
	for (const id of S.UI_SKIN_CHOICES) {
		assert.ok(Object.hasOwn(R.SKIN_PACK_REGISTRY, id), `no SKIN_PACK_REGISTRY entry for ${id}`);
		assert.equal(R.SKIN_PACK_REGISTRY[id].id, id);
	}
});

test("if a pack entry's layout differs from SKIN_LAYOUT[id], then the test names the id and both values", () => {
	for (const id of S.UI_SKIN_CHOICES) {
		const got = R.SKIN_PACK_REGISTRY[id]?.layout;
		const want = L.SKIN_LAYOUT[id];
		assert.deepEqual(
			got,
			want,
			`${id}: pack layout ${JSON.stringify(got)} != SKIN_LAYOUT ${JSON.stringify(want)}`
		);
	}
});

test("if parseSkinPackId('green-room') throws, then the test fails", () => {
	assert.equal(R.parseSkinPackId('green-room'), 'green-room');
	assert.equal(R.parseSkinPackId('practice-ddj-flx4'), 'practice-ddj-flx4');
});
