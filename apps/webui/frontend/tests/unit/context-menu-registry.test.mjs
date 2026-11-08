// W28 right-click registry harness (CTXMENU-02..04).
//
// 1. Every command id resolves to a REAL command: an IPC type that appears in
//    the PerformanceCommand union, or a method + path in the daemon's
//    openapi.json. Each check has a negative control so a broken parser cannot
//    pass by finding nothing.
// 2. Every resolved item is well formed over the pairwise context set.
// 3. The golden matrix per target kind matches (private path)/<kind>.md.
//    Regenerate with UPDATE_CONTEXT_MENU_GOLDENS=1.
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));
const REPO = fileURLToPath(new URL('../../../../..', import.meta.url));
const GOLDEN_DIR = `${REPO}(private path)`;
const UPDATE = process.env.UPDATE_CONTEXT_MENU_GOLDENS === '1';

let commands;
let registry;
let permutations;

before(async () => {
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	permutations = await loadTypeScriptModule('src/lib/context-menu/permutations.ts');
});

/** The `type: '...'` literals of the PerformanceCommand union, read from source. */
function performanceCommandTypes() {
	const source = readFileSync(`${FRONTEND}src/lib/rb/performance-ipc.svelte.ts`, 'utf8');
	const start = source.indexOf('export type PerformanceCommand =');
	assert.ok(start >= 0, 'PerformanceCommand union not found in performance-ipc.svelte.ts');
	const end = source.indexOf('\nexport ', start + 1);
	const block = source.slice(start, end);
	return new Set([...block.matchAll(/type: '([a-z0-9_]+)'/g)].map((m) => m[1]));
}

function openapiRoutes() {
	const spec = JSON.parse(readFileSync(`${REPO}apps/webui/openapi.json`, 'utf8'));
	const routes = new Set();
	for (const [path, ops] of Object.entries(spec.paths)) {
		for (const method of Object.keys(ops)) routes.add(`${method.toUpperCase()} ${path}`);
	}
	return routes;
}

test('the IPC parser reads the real union (positive and negative controls)', () => {
	const types = performanceCommandTypes();
	assert.ok(types.has('load') && types.has('hot_cue_clear') && types.has('rescue_stop_all'));
	assert.ok(!types.has('definitely_not_a_command'));
	assert.ok(types.size > 40, `only ${types.size} command types parsed`);
});

test('the openapi reader sees real routes (positive and negative controls)', () => {
	const routes = openapiRoutes();
	assert.ok(routes.has('POST /api/v1/tracks/{stable_id}:reveal'));
	assert.ok(!routes.has('POST /api/v1/tracks/{stable_id}:definitely-not'));
	assert.ok(routes.size > 100, `only ${routes.size} routes parsed`);
});

test('every menu command id names an existing IPC command or HTTP route', () => {
	const types = performanceCommandTypes();
	const routes = openapiRoutes();
	const missing = [];
	for (const [id, command] of Object.entries(commands.MENU_COMMANDS)) {
		const ok =
			command.via === 'ipc' ? types.has(command.type) : routes.has(`${command.method} ${command.path}`);
		if (!ok) missing.push(`${id} -> ${JSON.stringify(command)}`);
	}
	assert.deepEqual(missing, []);
});

test('every contribution names a command in the catalog', () => {
	for (const [kind, contributions] of Object.entries(registry.MENU_CONTRIBUTIONS)) {
		for (const c of contributions) {
			assert.ok(c.command in commands.MENU_COMMANDS, `${kind}/${c.id} -> unknown command ${c.command}`);
		}
	}
});

test('the context space and the registry cover the same target kinds', () => {
	assert.deepEqual(
		Object.keys(registry.MENU_CONTRIBUTIONS).sort(),
		[...permutations.MENU_TARGET_KINDS].sort()
	);
});

test('every resolved item is well formed across the pairwise contexts', () => {
	for (const kind of permutations.MENU_TARGET_KINDS) {
		const { rows } = permutations.resolveMatrix(kind);
		assert.ok(rows.length > 0, `${kind}: no contexts`);
		for (const { ctx, items } of rows) {
			const where = `${kind} ${JSON.stringify(ctx)}`;
			const ids = items.map((i) => i.id);
			assert.equal(new Set(ids).size, ids.length, `${where}: duplicate item ids ${ids}`);
			for (const item of items) {
				assert.ok(item.label.trim() !== '', `${where}/${item.id}: empty label`);
				assert.ok(item.title.trim() !== '', `${where}/${item.id}: empty title`);
				if (!item.enabled) {
					assert.ok((item.disabledReason ?? '').trim() !== '', `${where}/${item.id}: disabled with no reason`);
					assert.equal(item.title, item.disabledReason);
				} else {
					assert.equal(item.disabledReason, undefined, `${where}/${item.id}: enabled but carries a reason`);
				}
			}
		}
	}
});

test('pairwise rows cover every value pair of every kind', () => {
	for (const kind of permutations.MENU_TARGET_KINDS) {
		const space = permutations.MENU_CONTEXT_SPACE[kind];
		const fields = Object.keys(space);
		const { rows } = permutations.pairwiseContexts(kind);
		for (const field of fields) {
			for (const value of space[field]) {
				assert.ok(
					rows.some((row) => row[field] === value),
					`${kind}: no row has ${field}=${String(value)}`
				);
			}
		}
	}
	// Negative control: the track space really is pairwise, not the full product.
	const { rows, validCount } = permutations.pairwiseContexts('track');
	assert.ok(rows.length < validCount, `${rows.length} rows of ${validCount}`);
});

// Behavior the registry exists to guarantee; each names the inventory row on #5638.
test('USB and read-only rows offer deck loads only (inventory row 3)', () => {
	for (const source of ['usb', 'readonly-import']) {
		const items = registry.menuFor(
			{ kind: 'track', count: 1 },
			{ selection: 'one', source, audio: 'present', stems: 'none', workspace: 'performance' }
		);
		assert.deepEqual(
			items.map((i) => i.id),
			['load-1', 'load-2', 'load-3', 'load-4']
		);
	}
	// Control: a collection row has more than the loads.
	const collection = registry.menuFor(
		{ kind: 'track', count: 1 },
		{ selection: 'one', source: 'collection', audio: 'present', stems: 'none', workspace: 'performance' }
	);
	assert.ok(collection.length > 4);
});

test('a missing file disables loads and audio jobs with a reason (row 4)', () => {
	const items = registry.menuFor(
		{ kind: 'track', count: 1 },
		{ selection: 'one', source: 'collection', audio: 'missing', stems: 'none', workspace: 'performance' }
	);
	const byId = Object.fromEntries(items.map((i) => [i.id, i]));
	for (const id of ['load-1', 'analyze', 'stems-generate', 'lyrics', 'finder']) {
		assert.equal(byId[id].enabled, false, id);
		assert.match(byId[id].disabledReason, /missing/);
	}
	assert.equal(byId.relocate.enabled, true);
});

test('multi-select disables the one-row items and counts the batch ones (row 2)', () => {
	const items = registry.menuFor(
		{ kind: 'track', count: 3 },
		{ selection: 'many', source: 'playlist', audio: 'present', stems: 'none', workspace: 'performance' }
	);
	const byId = Object.fromEntries(items.map((i) => [i.id, i]));
	assert.equal(byId.relocate.enabled, false);
	assert.equal(byId['show-in-playlists'].enabled, false);
	assert.equal(byId['bulk-edit'].label, 'Bulk edit (3)');
	assert.equal(byId['remove-library'].label, 'Remove 3 tracks from library');
	assert.equal(byId['remove-playlist'].enabled, true);
});

test('Remove from playlist exists only inside a playlist', () => {
	const ids = registry
		.menuFor(
			{ kind: 'track', count: 1 },
			{ selection: 'one', source: 'collection', audio: 'present', stems: 'none', workspace: 'performance' }
		)
		.map((i) => i.id);
	assert.ok(!ids.includes('remove-playlist'));
});

test('tree folders get no Duplicate/Forbid duplicates and the root has no dead items (rows 6-8)', () => {
	assert.deepEqual(
		registry.menuFor({ kind: 'playlists-root' }, {}).map((i) => i.id),
		// LIBTREE-02: Sort A-Z is a live item with a route, not a dead one.
		['new-playlist', 'new-smartlist', 'sort-name']
	);
	assert.deepEqual(
		registry.menuFor({ kind: 'playlist-folder' }, {}).map((i) => i.id),
		['new-playlist', 'new-smartlist', 'rename', 'delete']
	);
	const smartlist = registry.menuFor({ kind: 'smartlist' }, {}).map((i) => i.id);
	assert.ok(!smartlist.includes('reveal'));
	const playlist = registry.menuFor({ kind: 'playlist' }, { forbidDuplicates: true });
	assert.equal(playlist.find((i) => i.id === 'forbid-duplicates').checked, true);
});

test('non-stick read-only trees stay empty; library source tabs offer refresh and settings (#9451)', () => {
	assert.deepEqual(registry.menuFor({ kind: 'tree-readonly' }, { stick: false }), []);
	const src = registry.menuFor({ kind: 'library-source' }, {});
	assert.deepEqual(
		src.map((i) => [i.id, i.enabled]),
		[
			['source-refresh', true],
			['source-finder', false],
			['source-settings', true]
		]
	);
	assert.ok((src.find((i) => i.id === 'source-finder')?.disabledReason ?? '').length > 0);
});

test('every target kind contributes at least one menu item somewhere (#9451)', () => {
	function kindsWithZeroItems(table) {
		const bad = [];
		for (const kind of permutations.MENU_TARGET_KINDS) {
			if ((table[kind] ?? []).length === 0) bad.push(kind);
		}
		return bad;
	}
	assert.deepEqual(kindsWithZeroItems(registry.MENU_CONTRIBUTIONS), []);
	const sparse = { ...registry.MENU_CONTRIBUTIONS, 'deck': [] };
	assert.deepEqual(kindsWithZeroItems(sparse), ['deck']);
	for (const kind of permutations.MENU_TARGET_KINDS) {
		const { rows } = permutations.resolveMatrix(kind);
		const ids = new Set(rows.flatMap((row) => row.items.map((i) => i.id)));
		assert.ok(ids.size > 0, `${kind}: resolved matrix is empty`);
	}
});

test('Library mode keeps deck loads (LIBWS-06) and drops worker starters (LIBWS-03, in the registry)', () => {
	const menu = (workspace) =>
		registry.menuFor(
			{ kind: 'track', count: 1 },
			{ selection: 'one', source: 'collection', audio: 'present', stems: 'none', workspace }
		);
	const ids = (workspace) => menu(workspace).map((i) => i.id);
	const gig = ids('performance');
	const library = ids('library');
	for (const id of ['analyze', 'stems-generate', 'lyrics']) {
		assert.ok(gig.includes(id), `${id} is offered in performance`);
		assert.ok(!library.includes(id), `${id} is hidden in Library mode`);
	}
	for (const deck of [1, 2, 3, 4]) {
		const id = `load-${deck}`;
		for (const workspace of ['performance', 'library']) {
			const items = menu(workspace);
			const loads = items.filter((i) => i.id === id);
			assert.equal(loads.length, 1, `${id} appears once in ${workspace}`);
			assert.equal(loads[0].enabled, true, `${id} stays enabled in ${workspace} when audio is present`);
		}
	}
	const missing = registry.menuFor(
		{ kind: 'track', count: 1 },
		{ selection: 'one', source: 'collection', audio: 'missing', stems: 'none', workspace: 'library' }
	);
	for (const deck of [1, 2, 3, 4]) {
		const item = missing.find((i) => i.id === `load-${deck}`);
		assert.equal(item?.enabled, false);
		assert.match(item?.disabledReason ?? '', /missing/);
	}
	assert.deepEqual(
		library,
		gig.filter((id) => !/^(analyze|stems-generate|lyrics)$/.test(id)),
		'Library mode hides only those items'
	);
});

test('track row parity set for collection rows in Gig (#9451)', () => {
	const ctx = {
		selection: 'one',
		source: 'collection',
		audio: 'present',
		stems: 'none',
		workspace: 'performance'
	};
	const ids = registry.menuFor({ kind: 'track', count: 1 }, ctx).map((i) => i.id);
	for (const id of [
		'load-1',
		'load-2',
		'load-3',
		'load-4',
		'add-playlist',
		'add-tag',
		'analyze',
		'track-info',
		'rate-0',
		'rate-1',
		'rate-2',
		'rate-3',
		'rate-4',
		'rate-5',
		'colour',
		'finder',
		'copy-path',
		'related-tracks',
		'remove-library'
	]) {
		assert.ok(ids.includes(id), `missing ${id}`);
	}
	const playlistIds = registry
		.menuFor({ kind: 'track', count: 1 }, { ...ctx, source: 'playlist' })
		.map((i) => i.id);
	assert.ok(playlistIds.includes('remove-playlist'));
});

test('multi-row selection disables Track info, Show in Finder and Related tracks (#9451)', () => {
	const { rows } = permutations.pairwiseContexts('track');
	for (const ctx of rows) {
		if (ctx.selection !== 'many') continue;
		const items = registry.menuFor({ kind: 'track', count: 3, rows: 3 }, ctx);
		const byId = Object.fromEntries(items.map((i) => [i.id, i]));
		for (const id of ['track-info', 'finder', 'related-tracks']) {
			const item = byId[id];
			if (item === undefined) continue;
			assert.equal(item.enabled, false, `${JSON.stringify(ctx)} ${id} should be disabled`);
			assert.match(item.disabledReason ?? '', /one track at a time/);
		}
	}
	const one = registry.menuFor(
		{ kind: 'track', count: 1 },
		{
			selection: 'one',
			source: 'collection',
			audio: 'present',
			stems: 'none',
			workspace: 'performance'
		}
	);
	const single = Object.fromEntries(one.map((i) => [i.id, i]));
	assert.equal(single['track-info'].enabled, true);
	assert.equal(single.finder.enabled, true);
});

test('perf-empty: load selection, layout checks and settings (#9451)', () => {
	const closed = registry.menuFor({ kind: 'perf-empty' }, { browser: false, view: 'waveform' });
	for (const deck of [1, 2, 3, 4]) {
		const item = closed.find((i) => i.id === `load-selected-${deck}`);
		assert.equal(item?.enabled, false);
		assert.ok((item?.disabledReason ?? '').includes('browser'));
	}
	const open = registry.menuFor({ kind: 'perf-empty' }, { browser: true, view: 'list' });
	for (const deck of [1, 2, 3, 4]) {
		assert.equal(open.find((i) => i.id === `load-selected-${deck}`)?.enabled, true);
	}
	const checked = open.filter((i) => i.id.startsWith('view-') && i.checked === true);
	assert.equal(checked.length, 1);
	assert.equal(checked[0].id, 'view-list');
	assert.equal(open.find((i) => i.id === 'settings')?.enabled, true);
});

test('Move is disabled with a reason when it would drop a loop or a color (Codex P1 on #5677)', () => {
	const target = { kind: 'hot-cue', deck: 1, slot: 'A' };
	const move = (moveLoses) =>
		registry.menuFor(target, { loaded: true, filled: true, moveLoses }).find((i) => i.id === 'cue-save');
	assert.equal(move('nothing').enabled, true, 'a plain or named cue still moves');
	assert.equal(move('loop').enabled, false);
	assert.match(move('loop').disabledReason, /loop - moving it would turn it into a point cue/);
	assert.equal(move('color').enabled, false);
	assert.match(move('color').disabledReason, /drop its color/);
	const set = registry.menuFor(target, { loaded: true, filled: false, moveLoses: 'nothing' });
	assert.equal(set.find((i) => i.id === 'cue-save').enabled, true, 'an empty slot still sets');
});

test('hot cue and mixer targets get their own items, not deck items (rows 13, 15)', () => {
	const cue = registry.menuFor({ kind: 'hot-cue', deck: 2, slot: 'C' }, { loaded: true, filled: true, moveLoses: 'nothing' });
	assert.deepEqual(
		cue.map((i) => i.label),
		['Jump to cue C', 'Move cue C to playhead', 'Delete cue C']
	);
	const mixer = registry.menuFor(
		{ kind: 'mixer-channel', deck: 3 },
		{ eqFlat: true, filterFlat: false, cue: true, assign: 'B' }
	);
	const byId = Object.fromEntries(mixer.map((i) => [i.id, i]));
	assert.equal(byId['eq-reset'].enabled, false);
	assert.equal(byId['filter-reset'].enabled, true);
	assert.equal(byId['assign-b'].checked, true);
	assert.equal(byId['assign-a'].checked, false);
});

test('trackAudioOf buckets listing availability', () => {
	assert.equal(registry.trackAudioOf({ file_availability: 'present', file_exists: true }), 'present');
	assert.equal(registry.trackAudioOf({ file_availability: 'absent' }), 'missing');
	assert.equal(registry.trackAudioOf({ file_exists: false }), 'missing');
	assert.equal(registry.trackAudioOf({ file_availability: 'spotify-streaming' }), 'streaming');
	assert.equal(registry.trackAudioOf({ is_streaming: true, file_exists: false }), 'streaming');
	assert.equal(registry.trackAudioOf({ file_availability: 'AVAILABILITY_PENDING' }), 'present');
});

test('golden matrices match (private path)/<kind>.md', () => {
	if (UPDATE) mkdirSync(GOLDEN_DIR, { recursive: true });
	const stale = [];
	for (const kind of permutations.MENU_TARGET_KINDS) {
		const path = `${GOLDEN_DIR}/${kind}.md`;
		const rendered = permutations.renderMatrix(kind);
		// Determinism control: a second render is byte-identical.
		assert.equal(permutations.renderMatrix(kind), rendered, `${kind}: render is not deterministic`);
		if (UPDATE) {
			writeFileSync(path, rendered);
			continue;
		}
		if (!existsSync(path) || readFileSync(path, 'utf8') !== rendered) stale.push(path);
	}
	assert.deepEqual(
		stale,
		[],
		'golden matrix out of date: rerun with UPDATE_CONTEXT_MENU_GOLDENS=1 and review the diff'
	);
});

test('USBEXP-04: a stick tree gets status, sync (disabled, says why) and eject; other read-only trees stay empty', () => {
	assert.deepEqual(registry.menuFor({ kind: 'tree-readonly' }, { stick: false }), []);
	const items = registry.menuFor({ kind: 'tree-readonly' }, { stick: true });
	assert.deepEqual(
		items.map((i) => [i.id, i.enabled]),
		[
			['usb-status', true],
			['usb-sync', false],
			['usb-eject', true]
		]
	);
	assert.match(items[1].disabledReason, /Export to USB/);
	assert.equal(items[2].command, 'usb.eject');
});

test('USBEXP-04: every playlist offers Export to USB, wired to the USB export plan route', () => {
	for (const forbidDuplicates of [false, true]) {
		const item = registry.menuFor({ kind: 'playlist' }, { forbidDuplicates }).find((i) => i.id === 'export-usb');
		assert.ok(item, 'Export to USB present');
		assert.equal(item.enabled, true);
		assert.equal(item.command, 'usb.export.plan');
	}
});
