import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Tags and tag types (Library Organization PRD R-TAG, R-TT, R-UNDO-1).
 *
 * [if] an instrument tag shows in the Tags cell with defaults [then] R-TT-2 is broken.
 * [if] turning a type's column off hides its tags everywhere [then] R-TT-2 is broken.
 * [if] a new type needs a code change to get a column [then] R-TT-1 is broken.
 * [if] the snooze expires late or never [then] R-TAG-4 is broken.
 * [if] one ⌘Z reverts both a tag change and a playlist edit [then] R-UNDO-1 is broken.
 * [if] a tag renders as a pill or with the wrong separator [then] R-TAG-1 is broken.
 */

const FRONTEND = fileURLToPath(new URL('../../', import.meta.url));
const type = (id, show_as_column, show_in_tags, sort_index, column_width = null) => ({
	id,
	label: id,
	singular: id,
	show_as_column,
	show_in_tags,
	sort_index,
	column_width
});
const TYPES = [type('general', 0, 1, 0), type('instrument', 1, 0, 1), type('mood', 0, 1, 2)];
const entry = (name, tagType) => ({
	name,
	name_norm: name.toLowerCase(),
	type: tagType,
	color: null,
	track_count: 1
});
const CATALOG = new Map(
	[entry('choir', 'instrument'), entry('dark', 'mood'), entry('Mozart', 'general')].map((tag) => [
		tag.name_norm,
		tag
	])
);
let tags;

before(async () => {
	tags = await loadTypeScriptModule('src/lib/rb/tag-display.ts');
});

describe('tag-display', () => {
	it('keeps an instrument out of the Tags cell by default', () => {
		assert.deepEqual(tags.tagsCellNames(['Mozart', 'choir', 'dark'], CATALOG, TYPES), [
			'Mozart',
			'dark'
		]);
	});

	it('treats an unknown tag as general', () => {
		assert.deepEqual(tags.tagsCellNames(['new'], CATALOG, TYPES), ['new']);
	});

	it('returns instruments to the Tags cell when their column is off', () => {
		const off = [type('general', 0, 1, 0), type('instrument', 0, 0, 1)];
		assert.deepEqual(tags.tagsCellNames(['choir'], CATALOG, off), ['choir']);
	});

	it('shows a type in both places when both switches are on', () => {
		const both = [type('general', 0, 1, 0), type('mood', 1, 1, 2)];
		assert.deepEqual(tags.tagsCellNames(['dark'], CATALOG, both), ['dark']);
		assert.deepEqual(tags.typeColumnNames(['dark'], 'mood', CATALOG), ['dark']);
	});

	it('builds columns from data, in order, never for general', () => {
		const custom = [...TYPES, type('era', 1, 1, 3, 90)];
		assert.deepEqual(
			tags.tagTypeColumnsOf(custom).map((col) => [col.id, col.width]),
			[
				['instrument', 120],
				['era', 90]
			]
		);
	});

	it('normalises width and case like the server', () => {
		assert.equal(tags.normalizeTagName('  ｐｉａｎｏ '), 'piano');
		assert.equal(tags.normalizeTagName('Piano'), 'piano');
	});

	it('snoozes for an hour and no longer', () => {
		const now = 1_000_000;
		const until = now + tags.TAG_REMOVE_SNOOZE_MS;
		assert.equal(tags.tagRemoveSnoozed(until, now + 59 * 60_000), true);
		assert.equal(tags.tagRemoveSnoozed(until, now + 61 * 60_000), false);
		assert.equal(tags.tagRemoveSnoozed(null, now), false);
	});

	it('sends one undo to the newest of the two stacks', () => {
		assert.equal(tags.undoTarget(null, null), 'playlist');
		assert.equal(tags.undoTarget(null, 5), 'playlist');
		assert.equal(tags.undoTarget(5, null), 'tag');
		assert.equal(tags.undoTarget(9, 5), 'tag');
		assert.equal(tags.undoTarget(5, 9), 'playlist');
	});

	it('names the undone action', () => {
		assert.equal(tags.undoToastMessage('tag.assign'), 'Undid tag change');
		assert.equal(tags.undoToastMessage('section.add'), 'Undid section.add');
	});

	it('groups by type and drops empty groups', () => {
		const groups = tags.groupTagsByType([entry('dark', 'mood'), entry('Mozart', 'general')], TYPES);
		assert.deepEqual(
			groups.map((group) => group.type.id),
			['general', 'mood']
		);
	});
});

describe('tag surfaces', () => {
	const source = (path) => readFileSync(join(FRONTEND, path), 'utf8');

	it('renders italic names with a middle dot, no pill background', () => {
		const cell = source('src/lib/components/rb/browser/TagCell.svelte');
		assert.match(cell, /' · '/);
		assert.match(cell, /<span class="sep"> · <\/span>/);
		assert.match(cell, /font-style: italic/);
		assert.doesNotMatch(cell, /\.tag\s*\{[^}]*background/);
	});

	it('offers Add tag on the track menu and hosts both dialogs', () => {
		assert.match(source('src/lib/context-menu/registry.ts'), /id: 'add-tag'/);
		const menu = source('src/lib/components/rb/browser/TrackContextMenu.svelte');
		assert.match(menu, /'add-tag':/);
		assert.match(menu, /<TagEditorPopover/);
		assert.match(menu, /<TagRemoveConfirm/);
	});

	it('has one Cmd+Z handler, the one that routes between the stacks', () => {
		const store = source('src/lib/rb/library-tags.svelte.ts');
		assert.doesNotMatch(store, /addEventListener\('keydown'/);
		const hotkeys = source('src/lib/rb/playlist-history-hotkeys.ts');
		assert.match(hotkeys, /undoTarget\(newestTagWriteAt\(\), newestPlaylistEditAt\(\)\)/);
		assert.match(hotkeys, /isTextEntryTarget/);
	});

	it('every tag write in the editor goes through the undoable routes', () => {
		const modal = source('src/lib/components/rb/MyTagEditorModal.svelte');
		assert.doesNotMatch(modal, /assignMyTags|renameMyTag|deleteMyTag/);
		const client = source('src/lib/rb/api-edit-suite.ts');
		assert.equal((client.match(/header: undoHeader/g) ?? []).length, 5);
	});
});
