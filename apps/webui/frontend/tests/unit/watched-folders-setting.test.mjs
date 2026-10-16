import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * FWATCH-01: the Settings watched-folders row describes a watcher that runs.
 *
 * Regression lines:
 * - if the row still reads "v2" or "not active" then a DJ is told folders are
 *   ignored while the folder-rescan scheduler is in fact scanning them
 * - if the notice stops saying the folders are only read then a DJ cannot tell
 *   whether Open DJ might change their files
 */

test('the watched-folders row reads as live and read-only', async () => {
	const { SETTINGS_CATALOG } = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const row = SETTINGS_CATALOG.find((s) => s.id === 'library_watcher_folders');
	assert.ok(row, 'if the row is gone then watched folders cannot be edited');
	assert.equal(row.implemented, true);
	assert.equal(row.control.kind, 'path_lines');
	const text = [row.label, row.title, row.detail, row.control.notice].join(' ');
	assert.doesNotMatch(text, /v2|not active|future release/i);
	assert.match(row.control.notice, /only reads these folders and never changes your files/);
});
