// Spec LIBM-104 (library-management-requirements.md section 09), issue #12326:
// system nodes (Recently deleted, Missing Tracks) draw in their own section
// with their own icons, and Rename and Delete stay visible on them but refuse.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let ssr;

before(async () => {
	ssr = await loadSvelteSsrModule(
		[
			"export { default as PlaylistTreeView } from '$lib/components/rb/browser/PlaylistTreeView.svelte';",
			"export { default as meta } from '$lib/components/rb/browser/PlaylistTreeView.stories';",
			"export { default as RecentlyDeleted } from '$lib/components/rb/browser/RecentlyDeletedFolderView.svelte';",
			"export * as sys from '$lib/components/rb/browser/system-tree-nodes';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const FOLDER_PATH = 'M1 3h5l1.5 2H15v8H1z';

function treeBody() {
	return ssr.render(ssr.PlaylistTreeView, { props: { ...ssr.meta.args } }).body;
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

function elementByAttr(html, attr) {
	const at = html.indexOf(attr);
	assert.ok(at >= 0, `missing ${attr}`);
	return elementAt(html, html.lastIndexOf('<', at));
}

test('[if] the Missing Tracks system node is rendered [then] it sits in the System section with its own icon, [else stop].', () => {
	const section = elementByAttr(treeBody(), 'data-testid="playlist-system-section"');
	assert.match(section, /role="group"/);
	assert.match(section, />System</);
	const row = elementByAttr(section, 'data-testid="playlist-missing-tracks"');
	assert.match(row, /data-system-node="missing"/);
	assert.ok(row.includes(ssr.sys.MISSING_TRACKS_ICON_PATH), 'Missing Tracks draws its system icon');
	assert.ok(!row.includes(FOLDER_PATH), 'Missing Tracks no longer borrows the folder icon');
	assert.doesNotMatch(row, /italic/);
});

test('[if] an ordinary playlist is rendered [then] it is outside the System section with live Rename and Delete, [else stop].', () => {
	const html = treeBody();
	const section = elementByAttr(html, 'data-testid="playlist-system-section"');
	assert.doesNotMatch(section, /data-testid="playlist-row"/);
	const row = elementByAttr(html, 'data-testid="playlist-row"');
	assert.doesNotMatch(row, /data-system-node/);
	assert.doesNotMatch(row, /aria-disabled/);
	assert.match(row, /aria-label="Rename playlist"/);
	assert.match(row, /title="Delete playlist"/);
});

test('[if] Rename is focused on a system node [then] it stays focusable and its description names why it refuses, [else stop].', () => {
	const row = elementByAttr(treeBody(), 'data-testid="playlist-missing-tracks"');
	const rename = elementByAttr(row, 'data-testid="system-node-rename"');
	assert.match(rename, /aria-disabled="true"/);
	assert.doesNotMatch(rename, /\sdisabled(=|\s|>)/, 'an HTML disabled button cannot take focus');
	const describedBy = /aria-describedby="([^"]+)"/.exec(rename)[1];
	const reason = ssr.sys.systemNodeLockReason('Missing Tracks');
	const described = elementByAttr(row, `id="${describedBy}"`);
	assert.ok(described.includes(`>${reason}</span>`), 'the description element carries the reason');
	assert.ok(rename.includes(`title="${reason}"`));
});

test('[if] Delete is invoked on a system node [then] nothing is deleted and the reason is returned, [else stop].', () => {
	const outcome = ssr.sys.invokeSystemNodeAction('delete', 'Missing Tracks');
	assert.deepEqual(outcome, {
		performed: false,
		action: 'delete',
		reason: ssr.sys.systemNodeLockReason('Missing Tracks')
	});
	const row = elementByAttr(treeBody(), 'data-testid="playlist-missing-tracks"');
	const del = elementByAttr(row, 'data-testid="system-node-delete"');
	assert.match(del, /aria-disabled="true"/);
	assert.match(del, /aria-label="Delete"/);
});

test('[if] Recently deleted is rendered [then] it is a system node with its bin icon and refused actions, [else stop].', () => {
	const html = ssr.render(ssr.RecentlyDeleted, {
		props: { open: false, rows: [], error: null, ontoggle: () => {}, onrestore: () => {} }
	}).body;
	const row = elementByAttr(html, 'data-testid="playlist-recently-deleted"');
	assert.match(row, /data-system-node="recently-deleted"/);
	assert.ok(row.includes(ssr.sys.RECENTLY_DELETED_ICON_PATH));
	assert.match(elementByAttr(row, 'data-testid="system-node-rename"'), /aria-disabled="true"/);
	assert.match(elementByAttr(row, 'data-testid="system-node-delete"'), /aria-disabled="true"/);
});
