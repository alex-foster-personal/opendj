// Spec LIBM-84 ((private path) section 08), issue #12307:
// smartlists in the tree draw as smartlists, and a track drag over one is
// refused before any playlist request, while ordinary playlists still take it.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let ssr;
let drag;

before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as PlaylistTreeView } from '$lib/components/rb/browser/PlaylistTreeView.svelte';",
			"export { default as meta, SmartlistDropRefused } from '$lib/components/rb/browser/PlaylistTreeView.stories';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
	drag = await loadTypeScriptModule('src/lib/rb/track-drag.svelte.ts');
});

function treeBody(extra = {}) {
	const args = { ...ssr.meta.args, ...ssr.SmartlistDropRefused.args, ...extra };
	return ssr.render(ssr.PlaylistTreeView, { props: args }).body;
}

/** The markup of the element that opens at `start`, up to its matching close tag. */
function elementAt(html, start) {
	const tag = /^<(\w+)/.exec(html.slice(start))[1];
	const open = new RegExp(`<${tag}[\\s>]|</${tag}>`, 'g');
	open.lastIndex = start;
	let depth = 0;
	for (let m = open.exec(html); m !== null; m = open.exec(html)) {
		depth += m[0].startsWith('</') ? -1 : 1;
		if (depth === 0) return html.slice(start, m.index + m[0].length);
	}
	throw new Error(`unclosed <${tag}> at ${start}`);
}

function rowOf(html, playlistId) {
	const at = html.indexOf(`data-playlist-id="${playlistId}"`);
	assert.ok(at >= 0, `missing row ${playlistId}`);
	return elementAt(html, html.lastIndexOf('<', at));
}

function fakeDragEvent() {
	const dataTransfer = { types: [], dropEffect: 'move' };
	let prevented = 0;
	return {
		dataTransfer,
		preventDefault: () => {
			prevented += 1;
		},
		get prevented() {
			return prevented;
		}
	};
}

test('[if] a smartlist sits beside ordinary playlists [then] it carries the smartlist kind, gear icon and label but no Rename, [else stop].', () => {
	const html = treeBody({ refusedDropId: null });
	const smart = rowOf(html, 'sl-1');
	assert.match(smart, /data-node-kind="smartlist"/);
	assert.match(smart, /data-testid="smartlist-icon"/);
	assert.match(smart, /Smartlist, fills from its rules:/);
	assert.match(smart, /title="Unplayed 128, smartlist: fills from its rules"/);
	assert.doesNotMatch(smart, /aria-label="Rename playlist"/);
	const plain = rowOf(html, 'pl-1');
	assert.match(plain, /data-node-kind="playlist"/);
	assert.doesNotMatch(plain, /smartlist-icon/);
	assert.match(plain, /aria-label="Rename playlist"/);
});

test('[if] tracks are dragged over a smartlist [then] the drop is refused without preventDefault and the row says why, [else stop].', () => {
	drag.beginTrackDrag(['ID-1', 'ID-2']);
	const event = fakeDragEvent();
	assert.equal(drag.trackDropRefusal('smartlist'), drag.SMARTLIST_DROP_REFUSAL);
	assert.equal(drag.refuseTrackDragOver(event), true);
	assert.equal(event.prevented, 0, 'no preventDefault means the browser never fires drop');
	assert.equal(event.dataTransfer.dropEffect, 'none');
	drag.endTrackDrag();
	const smart = rowOf(treeBody(), 'sl-1');
	assert.match(smart, /class="[^"]*\bdrop-refused\b/);
	assert.doesNotMatch(smart, /class="[^"]*\bdrop-target\b/);
	assert.ok(smart.includes(`title="${drag.SMARTLIST_DROP_REFUSAL}"`));
});

test('[if] tracks are dragged over a writable ordinary playlist [then] it accepts the drop and shows the outline, not a refusal, [else stop].', () => {
	assert.equal(drag.trackDropRefusal('playlist'), null);
	drag.beginTrackDrag(['ID-1']);
	const event = fakeDragEvent();
	assert.equal(drag.acceptTrackDragOver(event), true);
	assert.equal(event.prevented, 1);
	assert.equal(event.dataTransfer.dropEffect, 'copy');
	drag.endTrackDrag();
	const plain = rowOf(treeBody({ refusedDropId: null, dropTargetId: 'pl-3' }), 'pl-3');
	assert.match(plain, /class="[^"]*\bdrop-target\b/);
	assert.doesNotMatch(plain, /drop-refused/);
});

test('[if] no in-app track drag is running [then] a smartlist does not claim to refuse anything, [else stop].', () => {
	const event = fakeDragEvent();
	assert.equal(drag.refuseTrackDragOver(event), false);
	assert.equal(event.dataTransfer.dropEffect, 'move');
});
