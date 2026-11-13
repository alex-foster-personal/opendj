import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

let mod;
let registry;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/track-library-menu.ts');
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
});

test('confirm and toast strings mention files stay on disk', () => {
	assert.match(mod.removeFromLibraryConfirmMessage(1), /stays on disk/i);
	assert.match(mod.removeFromLibraryConfirmMessage(3), /stay on disk/i);
	assert.match(mod.removeFromLibraryToastMessage(1), /stays on disk/i);
	assert.match(mod.removeFromLibraryToastMessage(3), /stay on disk/i);
});

test('menu item carries run and forwards selected ids', () => {
	const seen = [];
	const item = mod.removeFromLibraryMenuItem(['a', 'b'], (ids) => seen.push(...ids));
	assert.equal(item.id, 'remove-library');
	assert.equal(typeof item.run, 'function');
	item.run();
	assert.deepEqual(seen, ['a', 'b']);
});

test('TrackContextMenu and BrowserPanel wire remove-from-library helpers', () => {
	const menu = readFileSync(`${SRC}/lib/components/rb/browser/TrackContextMenu.svelte`, 'utf8');
	const panel = readFileSync(`${SRC}/lib/components/rb/BrowserPanel.svelte`, 'utf8');
	// W28: the registry decides visibility; the host binds the id to the handler.
	assert.match(
		menu,
		/'remove-library': onremovefromlibrary \? \(\) => onremovefromlibrary\(targetIds\) : undefined/
	);
	assert.match(panel, /onremovefromlibrary=\{\(ids\) => void removeFromLibraryUi\(ids\)\}/);
	assert.match(panel, /confirmRemoveFromLibrary/);
	assert.match(panel, /removeFromLibraryToastMessage/);
});

// Issue #12325 (LIBM-101b): every library-row remove names its database-only scope.

test('one track removed from library names the database-only scope', () => {
	/** [if] one track is removed from the library menu [then] the confirm and tooltip say database record only and file not deleted, [else stop]. */
	const confirm = mod.removeFromLibraryConfirmMessage(1);
	assert.match(confirm, /database record only/);
	assert.match(confirm, /audio file is not deleted/);
	const item = registryItem(1, 'collection', 'remove-library');
	assert.equal(item.label, 'Remove from library');
	assert.match(item.title, /database record only/);
	assert.match(item.title, /audio file is not deleted/);
});

test('multiple tracks removed from library name the database-only scope', () => {
	/** [if] multiple tracks are removed from the library menu [then] the plural confirm and tooltip keep the database-only wording, [else stop]. */
	const confirm = mod.removeFromLibraryConfirmMessage(3);
	assert.match(confirm, /^Remove 3 tracks from the library\?/);
	assert.match(confirm, /database records only/);
	assert.match(confirm, /audio files are not deleted/);
	const item = registryItem(3, 'collection', 'remove-library');
	assert.equal(item.label, 'Remove 3 tracks from library');
	assert.match(item.title, /database record only/);
});

test('remove from playlist keeps distinct wording from remove from library', () => {
	/** [if] a track is removed only from a playlist [then] its tooltip says playlist only and never claims a database removal, [else stop]. */
	const item = registryItem(1, 'playlist', 'remove-playlist');
	assert.equal(item.label, 'Remove from playlist');
	assert.match(item.title, /from this playlist only/);
	assert.match(item.title, /stays in the library/);
	assert.doesNotMatch(item.title, /database record/);
	assert.notEqual(mod.REMOVE_FROM_PLAYLIST_SCOPE, mod.REMOVE_FROM_LIBRARY_SCOPE);
});

test('cancelled remove from library removes nothing', () => {
	/** [if] the remove-from-library confirm is cancelled [then] no ids come back to remove, [else stop]. */
	const prompts = [];
	const cancelled = mod.confirmRemoveFromLibrary(['a', 'b', 'a'], (message) => {
		prompts.push(message);
		return false;
	});
	assert.deepEqual(cancelled, []);
	assert.equal(prompts.length, 1);
	assert.match(prompts[0], /database records only/);
	assert.deepEqual(
		mod.confirmRemoveFromLibrary(['a', 'b', 'a'], () => true),
		['a', 'b']
	);
	assert.deepEqual(
		mod.confirmRemoveFromLibrary([], () => true),
		[]
	);
});

function registryItem(count, source, id) {
	const items = registry.menuFor(
		{ kind: 'track', count },
		{
			selection: count > 1 ? 'many' : 'one',
			source,
			audio: 'present',
			stems: 'none',
			workspace: 'performance'
		}
	);
	const item = items.find((i) => i.id === id);
	assert.ok(item, `missing menu item ${id}`);
	return item;
}

// #12578: Re-analyze menu action startup failures (kept in its own block at the end of the file).
const REANALYZE_GROUP = 'reanalyze-batch';

/** Build runReanalyze deps that record toasts and watcher starts. */
function reanalyzeDeps(overrides) {
	const toasts = [];
	const watched = [];
	const deps = {
		enqueueBackfill: async () => ({ admitted: 1, offered: 1, refused: 0, batch_id: 'batch-9' }),
		backfillProgress: async () => null,
		pushToast: (message, kind, _dismiss, _cause, _ctx, groupKey) =>
			toasts.push({ message, kind, groupKey }),
		watchBatch: (batchId) => {
			watched.push(batchId);
			return () => {};
		},
		...overrides
	};
	return { deps, toasts, watched };
}

test('reanalyze: queue submission rejects shows the error and no in-progress toast', async () => {
	const ctx = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
	const { deps, toasts, watched } = reanalyzeDeps({
		enqueueBackfill: async () => {
			throw new Error('queue offline');
		}
	});
	await ctx.runReanalyze(['a'], deps);
	assert.deepEqual(toasts, [
		{ message: 'queue offline', kind: 'error', groupKey: REANALYZE_GROUP }
	]);
	assert.deepEqual(watched, []);
});

test('reanalyze: progress lookup rejects still reports the queued batch and watches it', async () => {
	const ctx = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
	const { deps, toasts, watched } = reanalyzeDeps({
		backfillProgress: async () => {
			throw new Error('progress 503');
		}
	});
	await ctx.runReanalyze(['a'], deps);
	assert.equal(toasts.length, 1);
	assert.equal(toasts[0].kind, 'info');
	assert.match(toasts[0].message, /queued 1 of 1 \(batch batch-9\)/);
	assert.doesNotMatch(toasts[0].message, /progress 503/);
	assert.deepEqual(watched, ['batch-9']);
});

test('reanalyze: submission and subscription succeed shows one queued toast then watches', async () => {
	const ctx = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
	const { deps, toasts, watched } = reanalyzeDeps({});
	await ctx.runReanalyze(['a'], deps);
	assert.equal(toasts.length, 1);
	assert.equal(toasts[0].kind, 'info');
	assert.equal(toasts[0].groupKey, REANALYZE_GROUP);
	assert.deepEqual(watched, ['batch-9']);
});

test('reanalyze: subscription throwing replaces the in-progress toast with an error', async () => {
	const ctx = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
	const { deps, toasts } = reanalyzeDeps({
		watchBatch: () => {
			throw new Error('subscribe failed');
		}
	});
	await ctx.runReanalyze(['a'], deps);
	assert.equal(toasts.length, 2);
	assert.equal(toasts[0].groupKey, REANALYZE_GROUP);
	assert.deepEqual(toasts[1], {
		message: 'subscribe failed',
		kind: 'error',
		groupKey: REANALYZE_GROUP
	});
});

// #12577: Reveal in Finder runner reports partial multi-selection failures honestly.
let ctxMenu;

before(async () => {
	ctxMenu = await loadTypeScriptModule('src/lib/components/rb/browser/track-context-menu.ts');
});

/** Run runRevealTracks with ids in failIds rejecting, returning toasts and attempted ids. */
async function runReveal(ids, failIds) {
	const seen = [];
	const toasts = [];
	await ctxMenu.runRevealTracks(ids, {
		revealTrack: async (id) => {
			seen.push(id);
			if (failIds.includes(id)) throw new Error(`reveal failed for ${id}`);
		},
		pushToast: (message, kind) => toasts.push({ message, kind })
	});
	return { seen, toasts };
}

test('reveal: one failure after a success reports partial count and first error', async () => {
	const { seen, toasts } = await runReveal(['a', 'b', 'c'], ['b', 'c']);
	assert.deepEqual(seen, ['a', 'b', 'c']);
	assert.deepEqual(toasts, [
		{ message: 'Revealed 1 of 3 tracks', kind: 'info' },
		{ message: 'reveal failed for b', kind: 'error' }
	]);
});

test('reveal: every selected track fails reports only the first error', async () => {
	const { seen, toasts } = await runReveal(['a', 'b'], ['a', 'b']);
	assert.deepEqual(seen, ['a', 'b']);
	assert.deepEqual(toasts, [{ message: 'reveal failed for a', kind: 'error' }]);
});

// Issue #12303 (LIBM-54): undelete control for the deleted-tracks view.

test('deleted track offers Restore to library and no writable items', () => {
	/** [if] a deleted track is selected in the deleted-tracks view [then] the menu offers Restore to library and hides Remove from library, [else stop]. */
	const item = registryItem(1, 'deleted', 'restore-library');
	assert.equal(item.label, 'Restore to library');
	assert.equal(item.command, 'track.undelete');
	assert.equal(item.enabled, true);
	assert.match(item.title, /:undelete/);
	const ids = registry.menuFor({ kind: 'track', count: 1 }, deletedCtx()).map((i) => i.id);
	assert.ok(!ids.includes('remove-library'));
	const live = registry.menuFor(
		{ kind: 'track', count: 1 },
		{ ...deletedCtx(), source: 'collection' }
	);
	assert.ok(!live.map((i) => i.id).includes('restore-library'));
});

test('successful restore reports every restored track', async () => {
	/** [if] restore succeeds for a deleted track [then] each id is undeleted once and an info toast names the count, [else stop]. */
	const called = [];
	const outcome = await mod.restoreDeletedTracks(['a', 'b', 'a'], async (id) => {
		called.push(id);
		return { stable_id: id, deleted_at: null, memberships: [] };
	});
	assert.deepEqual(called, ['a', 'b']);
	assert.deepEqual(outcome.restored, ['a', 'b']);
	assert.equal(outcome.tone, 'info');
	assert.equal(outcome.message, 'Restored 2 tracks to the library.');
	const one = await mod.restoreDeletedTracks(['a'], async () => ({}));
	assert.equal(one.message, 'Restored 1 track to the library.');
});

test('restore of an already-live or missing track reports the failure', async () => {
	/** [if] restore fails because the track is already live or missing [then] the outcome is an error toast with the server reason, [else stop]. */
	const outcome = await mod.restoreDeletedTracks(['live', 'gone', 'ok'], async (id) => {
		if (id === 'live') throw new Error('track live is not deleted');
		if (id === 'gone') throw new Error('track gone not found');
		return {};
	});
	assert.deepEqual(outcome.restored, ['ok']);
	assert.deepEqual(
		outcome.failed.map((f) => f.stableId),
		['live', 'gone']
	);
	assert.equal(outcome.tone, 'error');
	assert.match(
		outcome.message,
		/Restore failed for 2 tracks \(already in the library or missing\)/
	);
	assert.match(outcome.message, /track live is not deleted/);
});

function deletedCtx() {
	return {
		selection: 'one',
		source: 'deleted',
		audio: 'present',
		stems: 'none',
		tracklistHidden: 'shown',
		workspace: 'performance'
	};
}
