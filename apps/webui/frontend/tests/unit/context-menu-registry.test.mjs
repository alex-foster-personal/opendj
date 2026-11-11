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
let deckMenuCommands;
let deckMove;
let mixerActions;
let targetOfMod;

before(async () => {
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	permutations = await loadTypeScriptModule('src/lib/context-menu/permutations.ts');
	deckMenuCommands = await loadTypeScriptModule('src/lib/context-menu/deck-menu-commands.ts');
	deckMove = await loadTypeScriptModule('src/lib/rb/deck-move.ts');
	mixerActions = await loadTypeScriptModule('src/lib/context-menu/mixer-actions.ts');
	targetOfMod = await loadTypeScriptModule('src/lib/context-menu/target-of.ts');
});

class El {
	constructor(tag) {
		this.tagName = String(tag).toUpperCase();
		this.parentElement = null;
		this.children = [];
		this.attrs = new Map();
	}

	appendChild(child) {
		child.parentElement = this;
		this.children.push(child);
		return child;
	}

	setAttribute(name, value) {
		this.attrs.set(name, String(value));
	}

	getAttribute(name) {
		return this.attrs.has(name) ? this.attrs.get(name) : null;
	}

	hasAttribute(name) {
		return this.attrs.has(name);
	}

	matches(selector) {
		return selector.split(',').some((part) => this._one(part.trim()));
	}

	_one(selector) {
		if (selector.startsWith('[') && selector.endsWith(']')) {
			const body = selector.slice(1, -1);
			const eq = body.indexOf('=');
			if (eq === -1) return this.hasAttribute(body);
			const key = body.slice(0, eq);
			const val = body.slice(eq + 1).replace(/^"|"$/g, '');
			return this.getAttribute(key) === val;
		}
		return this.tagName.toLowerCase() === selector.toLowerCase();
	}

	closest(selector) {
		let n = this;
		while (n) {
			if (n.matches(selector)) return n;
			n = n.parentElement;
		}
		return null;
	}

	contains(other) {
		let n = other;
		while (n) {
			if (n === this) return true;
			n = n.parentElement;
		}
		return false;
	}
}

/** The `type: '...'` literals of the PerformanceCommand union, read from source. */
function performanceCommandTypes() {
	const source = readFileSync(`${FRONTEND}src/lib/rb/performance-ipc-types.ts`, 'utf8');
	const start = source.indexOf('export type PerformanceCommand =');
	assert.ok(start >= 0, 'PerformanceCommand union not found in performance-ipc-types.ts');
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
			command.via === 'ipc'
				? types.has(command.type)
				: routes.has(`${command.method} ${command.path}`);
		if (!ok) missing.push(`${id} -> ${JSON.stringify(command)}`);
	}
	assert.deepEqual(missing, []);
});

test('every contribution names a command in the catalog', () => {
	for (const [kind, contributions] of Object.entries(registry.MENU_CONTRIBUTIONS)) {
		for (const c of contributions) {
			assert.ok(
				c.command in commands.MENU_COMMANDS,
				`${kind}/${c.id} -> unknown command ${c.command}`
			);
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
					assert.ok(
						(item.disabledReason ?? '').trim() !== '',
						`${where}/${item.id}: disabled with no reason`
					);
					assert.equal(item.title, item.disabledReason);
				} else {
					assert.equal(
						item.disabledReason,
						undefined,
						`${where}/${item.id}: enabled but carries a reason`
					);
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
		{
			selection: 'one',
			source: 'collection',
			audio: 'present',
			stems: 'none',
			workspace: 'performance'
		}
	);
	assert.ok(collection.length > 4);
});

test('a missing file disables loads and audio jobs with a reason (row 4)', () => {
	const items = registry.menuFor(
		{ kind: 'track', count: 1 },
		{
			selection: 'one',
			source: 'collection',
			audio: 'missing',
			stems: 'none',
			workspace: 'performance'
		}
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
		{
			selection: 'many',
			source: 'playlist',
			audio: 'present',
			stems: 'none',
			workspace: 'performance'
		}
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
			{
				selection: 'one',
				source: 'collection',
				audio: 'present',
				stems: 'none',
				workspace: 'performance'
			}
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
	const sparse = { ...registry.MENU_CONTRIBUTIONS, deck: [] };
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
			assert.equal(
				loads[0].enabled,
				true,
				`${id} stays enabled in ${workspace} when audio is present`
			);
		}
	}
	const missing = registry.menuFor(
		{ kind: 'track', count: 1 },
		{
			selection: 'one',
			source: 'collection',
			audio: 'missing',
			stems: 'none',
			workspace: 'library'
		}
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
		registry
			.menuFor(target, { loaded: true, filled: true, moveLoses })
			.find((i) => i.id === 'cue-save');
	assert.equal(move('nothing').enabled, true, 'a plain or named cue still moves');
	assert.equal(move('loop').enabled, false);
	assert.match(move('loop').disabledReason, /loop - moving it would turn it into a point cue/);
	assert.equal(move('color').enabled, false);
	assert.match(move('color').disabledReason, /drop its color/);
	const set = registry.menuFor(target, { loaded: true, filled: false, moveLoses: 'nothing' });
	assert.equal(set.find((i) => i.id === 'cue-save').enabled, true, 'an empty slot still sets');
});

test('hot cue and mixer targets get their own items, not deck items (rows 13, 15)', () => {
	const cue = registry.menuFor(
		{ kind: 'hot-cue', deck: 2, slot: 'C' },
		{ loaded: true, filled: true, moveLoses: 'nothing' }
	);
	assert.deepEqual(
		cue.map((i) => i.label),
		['Jump to cue C', 'Move cue C to playhead', 'Delete cue C']
	);
	const mixer = registry.menuFor(
		{ kind: 'mixer-channel', deck: 3 },
		{ eqFlat: true, filterFlat: false, cue: true, assign: 'B', trimUnity: false, autoGain: 'none' }
	);
	const byId = Object.fromEntries(mixer.map((i) => [i.id, i]));
	assert.equal(byId['eq-reset'].enabled, false);
	assert.equal(byId['filter-reset'].enabled, true);
	assert.equal(byId['assign-b'].checked, true);
	assert.equal(byId['assign-a'].checked, false);
	assert.ok(byId['trim-auto-gain'], 'trim-auto-gain on mixer channel');
});

/** Stands in for (private path) (#9447 not merged). */
const EPIC_MIXER_REF = {
	'mixer-channel': [
		{ feature: 'Reset EQ', id: 'eq-reset' },
		{ feature: 'Reset filter', id: 'filter-reset' },
		{ feature: 'Headphone cue', id: 'channel-cue' },
		{ feature: 'Crossfader assign', id: 'assign-a' },
		{ feature: 'Auto gain', id: 'trim-auto-gain' },
		{
			feature: 'Kill band',
			notApplicable: true,
			reason: 'a strip names no band; Kill is on the EQ dial'
		}
	],
	'mixer-control': [
		{
			feature: 'Reset to default',
			ids: [
				'reset-eq',
				'reset-trim',
				'reset-filter',
				'reset-fader',
				'reset-crossfader',
				'reset-stem'
			]
		},
		{ feature: 'Kill band', id: 'eq-kill' },
		{ feature: 'Auto gain', id: 'trim-auto-gain' },
		{
			feature: 'Assign',
			notApplicable: true,
			reason: "assign is a channel fact; the dial menu appends its strip's items"
		}
	],
	stem: [
		{ feature: 'Mute', id: 'stem-mute' },
		{ feature: 'Solo', id: 'stem-solo' },
		{ feature: 'Reset levels', id: 'stem-levels-reset' },
		{ feature: 'Generate stems', id: 'stems-tiers' }
	]
};

function epicCoverageRatio(kind, ref = EPIC_MIXER_REF[kind]) {
	const { rows } = permutations.resolveMatrix(kind);
	const shown = new Set();
	for (const row of rows) for (const item of row.items) shown.add(item.id);
	let applicable = 0;
	let present = 0;
	for (const entry of ref) {
		if (entry.notApplicable) {
			assert.ok(entry.reason?.trim(), `${kind} ${entry.feature}: empty not-applicable reason`);
			continue;
		}
		applicable += 1;
		const ids = entry.ids ?? [entry.id];
		if (ids.some((id) => shown.has(id))) present += 1;
	}
	return { present, applicable };
}

test('stem lane resolves to kind stem (targetOf fixture DOM)', () => {
	const row = new El('div');
	row.setAttribute('data-deck', '2');
	row.setAttribute('data-wave-surface', 'row');
	const stack = new El('div');
	stack.className = 'stem-wave-stack';
	const lane = new El('div');
	lane.setAttribute('data-stem', 'drums');
	row.appendChild(stack);
	stack.appendChild(lane);
	assert.deepEqual(targetOfMod.targetOf(lane, null), { kind: 'stem', deck: 2, stem: 'drums' });
	const wave = new El('div');
	wave.setAttribute('data-wave-surface', 'main');
	row.appendChild(wave);
	assert.deepEqual(targetOfMod.targetOf(wave, null), { kind: 'waveform', deck: 2 });
	lane.setAttribute('data-stem', 'nonsense');
	assert.deepEqual(targetOfMod.targetOf(lane, null), { kind: 'waveform', deck: 2 });
	const chipChannel = new El('div');
	chipChannel.setAttribute('data-mixer-channel', '1');
	const chip = new El('div');
	chip.setAttribute('data-performance-control', 'vocal');
	chipChannel.appendChild(chip);
	assert.deepEqual(targetOfMod.targetOf(chip, null), { kind: 'mixer-channel', deck: 1 });
	const dialChannel = new El('div');
	dialChannel.setAttribute('data-mixer-channel', '1');
	const dial = new El('div');
	dial.setAttribute('data-knob-id', '1:low');
	dialChannel.appendChild(dial);
	const dialTarget = targetOfMod.targetOf(dial, null);
	assert.equal(dialTarget.kind, 'mixer-control');
	assert.equal(dialTarget.control, 'eq_low');
	assert.equal(dialTarget.channel, 1);
	assert.deepEqual(targetOfMod.targetOf(null, null), { kind: 'perf-empty' });
});

test('reset on an EQ dial sends the default value', () => {
	assert.deepEqual(mixerActions.controlResetCommand('eq_low', 1), {
		type: 'eq',
		deck: 1,
		band: 'low',
		value: 0.5
	});
	assert.equal(mixerActions.controlDefault('fader'), 1);
	assert.equal(mixerActions.controlDefault('crossfader'), 0.5);
	assert.deepEqual(mixerActions.controlResetCommand('crossfader', null), {
		type: 'crossfader',
		value: 0.5
	});
	assert.deepEqual(mixerActions.controlKillCommand('eq_high', 3), {
		type: 'eq',
		deck: 3,
		band: 'high',
		value: 0
	});
	assert.throws(() => mixerActions.controlKillCommand('trim', 1));
	const items = registry.menuFor(
		{ kind: 'mixer-control', label: 'EQ LOW' },
		{ animating: false, control: 'eq', value: 'other', autoGain: 'none' }
	);
	const byId = Object.fromEntries(items.map((i) => [i.id, i]));
	assert.equal(byId['reset-eq'].enabled, true);
	assert.equal(byId['reset-eq'].command, 'mixer.eq');
	assert.match(byId['reset-eq'].label, /Reset EQ LOW to default/);
	assert.ok(byId['eq-kill']);
	assert.equal(byId['trim-auto-gain'], undefined);
	const flat = registry.menuFor(
		{ kind: 'mixer-control', label: 'EQ LOW' },
		{ animating: false, control: 'eq', value: 'default', autoGain: 'none' }
	);
	assert.match(flat.find((i) => i.id === 'reset-eq').disabledReason, /already at its default/);
});

test('trim auto gain commands and registry reasons', () => {
	assert.deepEqual(mixerActions.trimAutoGainCommands(1, 'user', false, 'sid'), [
		{ type: 'trim', deck: 1, value: 0.5 },
		{ type: 'auto_gain_offset', deck: 1, offset_db: null, stable_id: 'sid' }
	]);
	assert.deepEqual(mixerActions.trimAutoGainCommands(1, 'auto', true), []);
	const trimItem = registry
		.menuFor(
			{ kind: 'mixer-channel', deck: 1 },
			{
				eqFlat: false,
				filterFlat: false,
				cue: false,
				assign: 'A',
				trimUnity: true,
				autoGain: 'auto'
			}
		)
		.find((i) => i.id === 'trim-auto-gain');
	assert.match(trimItem.disabledReason, /trim already follows auto gain/);
	const noneItem = registry
		.menuFor(
			{ kind: 'mixer-channel', deck: 1 },
			{
				eqFlat: false,
				filterFlat: false,
				cue: false,
				assign: 'A',
				trimUnity: false,
				autoGain: 'none'
			}
		)
		.find((i) => i.id === 'trim-auto-gain');
	assert.match(noneItem.disabledReason, /no auto gain on this deck/);
	assert.equal(mixerActions.autoGainFactOf({ state: 'empty' }), 'none');
});

test('stem lane menu items', () => {
	const target = { kind: 'stem', deck: 1, stem: 'vocal' };
	const ready = registry.menuFor(target, {
		loaded: true,
		stems: 'ready',
		available: true,
		muted: true,
		solo: false,
		levelsUnity: false
	});
	assert.deepEqual(
		ready.map((i) => i.id),
		['stem-mute', 'stem-solo', 'stem-levels-reset', 'stems-tiers']
	);
	assert.equal(ready[0].checked, true);
	assert.match(ready[0].label, /Mute vocals/);
	const noStems = registry.menuFor(target, {
		loaded: true,
		stems: 'none',
		available: true,
		muted: false,
		solo: false,
		levelsUnity: true
	});
	const reason = /use Generate stems/;
	for (const id of ['stem-mute', 'stem-solo', 'stem-levels-reset']) {
		assert.match(noStems.find((i) => i.id === id).disabledReason, reason);
	}
	assert.equal(noStems.find((i) => i.id === 'stems-tiers').enabled, true);
	const empty = registry.menuFor(target, {
		loaded: false,
		stems: 'none',
		available: false,
		muted: false,
		solo: false,
		levelsUnity: true
	});
	for (const item of empty) assert.match(item.disabledReason, /CH1 is empty/);
});

test('mixer and stem epic coverage at least 80 percent (path A stand-in)', () => {
	for (const kind of ['mixer-channel', 'mixer-control', 'stem']) {
		const { present, applicable } = epicCoverageRatio(kind);
		assert.ok(present / applicable >= 0.8, `${kind}: ${present}/${applicable}`);
	}
	const broken = {
		...EPIC_MIXER_REF,
		'mixer-channel': EPIC_MIXER_REF['mixer-channel'].map((e) =>
			e.id === 'eq-reset' ? { ...e, id: 'eq-reset-missing' } : e
		)
	};
	const { present, applicable } = epicCoverageRatio('mixer-channel', broken['mixer-channel']);
	assert.equal(`${present}/${applicable}`, '4/5');
});

test('trackAudioOf buckets listing availability', () => {
	assert.equal(
		registry.trackAudioOf({ file_availability: 'present', file_exists: true }),
		'present'
	);
	assert.equal(registry.trackAudioOf({ file_availability: 'absent' }), 'missing');
	assert.equal(registry.trackAudioOf({ file_exists: false }), 'missing');
	assert.equal(registry.trackAudioOf({ file_availability: 'spotify-streaming' }), 'streaming');
	assert.equal(registry.trackAudioOf({ is_streaming: true, file_exists: false }), 'streaming');
	assert.equal(registry.trackAudioOf({ file_availability: 'AVAILABILITY_PENDING' }), 'present');
	assert.equal(
		registry.trackAudioOf({ deck_load_refusal: 'cloud_policy_unconfigured', file_exists: false }),
		'cloud_unconfigured'
	);
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
		const item = registry
			.menuFor({ kind: 'playlist' }, { forbidDuplicates })
			.find((i) => i.id === 'export-usb');
		assert.ok(item, 'Export to USB present');
		assert.equal(item.enabled, true);
		assert.equal(item.command, 'usb.export.plan');
	}
});

const TRACK_BOUND_DECK = [
	'play',
	'unload',
	'beat-sync',
	'make-master',
	'move-to-2',
	'move-to-3',
	'move-to-4',
	'find-in-library',
	'track-info',
	'reanalyze',
	'grid-lock'
];

const DECK_CTX_EMPTY_SELECTION = {
	loaded: false,
	playing: false,
	looping: false,
	synced: false,
	master: false,
	selection: true,
	keyLock: false,
	quantize: false,
	slip: false,
	otherLoaded: []
};

const deckMenu = (ctx) => registry.menuFor({ kind: 'deck', deck: 1 }, ctx);
const byId = (items) => Object.fromEntries(items.map((i) => [i.id, i]));

test('#9448: an empty deck offers Load selected track and disables every track-bound item with a reason', () => {
	const { rows } = permutations.pairwiseContexts('deck');
	let sawSelectionTrue = false;
	for (const row of rows) {
		if (row.loaded !== false) continue;
		if (row.selection === true) sawSelectionTrue = true;
		const items = byId(deckMenu(row));
		assert.equal(items['exit-loop'], undefined, 'exit-loop hidden on empty deck');
		const loadSel = items['load-selection'];
		assert.ok(loadSel, 'load-selection present');
		assert.equal(loadSel.enabled, row.selection);
		if (!row.selection) {
			assert.match(loadSel.disabledReason, /no track is selected/);
		}
		for (const id of TRACK_BOUND_DECK) {
			assert.ok(items[id], id);
			assert.equal(items[id].enabled, false, `${id} on empty deck`);
			assert.match(items[id].disabledReason, /CH1 is empty/);
		}
	}
	assert.ok(sawSelectionTrue, 'pairwise deck contexts include selection:true on empty deck');
	const explicit = deckMenu(DECK_CTX_EMPTY_SELECTION);
	assert.equal(explicit.find((i) => i.id === 'load-selection').enabled, true);
	const loadedStopped = deckMenu({
		loaded: true,
		playing: false,
		looping: false,
		synced: false,
		master: false,
		selection: false,
		keyLock: false,
		quantize: false,
		slip: false,
		otherLoaded: []
	});
	const loadedById = byId(loadedStopped);
	for (const id of TRACK_BOUND_DECK) {
		if (id === 'grid-lock') {
			assert.equal(loadedById[id].enabled, false);
			assert.match(loadedById[id].disabledReason, /beatgrid lock is not built yet/);
		} else if (id === 'make-master') {
			assert.equal(loadedById[id].enabled, true);
		} else {
			assert.equal(loadedById[id].enabled, true, id);
		}
	}
});

test('#9448: every new enabled deck item names a catalog command that resolves', () => {
	const types = performanceCommandTypes();
	const routes = openapiRoutes();
	const missing = [];
	for (const row of permutations.pairwiseContexts('deck').rows) {
		for (const item of deckMenu(row)) {
			if (!item.enabled) continue;
			const command = commands.MENU_COMMANDS[item.command];
			const ok =
				command.via === 'ipc'
					? types.has(command.type)
					: routes.has(`${command.method} ${command.path}`);
			if (!ok) missing.push(`${row} ${item.id} -> ${item.command}`);
		}
	}
	assert.deepEqual(missing, []);
});

test('#9448: instant double needs a loaded source and refuses a playing target', () => {
	const withSource = byId(
		deckMenu({
			loaded: true,
			playing: false,
			looping: false,
			synced: false,
			master: false,
			selection: false,
			keyLock: false,
			quantize: false,
			slip: false,
			otherLoaded: [2]
		})
	);
	assert.equal(withSource['double-from-2'].enabled, true);
	assert.equal(withSource['double-from-3'].enabled, false);
	assert.match(withSource['double-from-3'].disabledReason, /CH3 is empty/);
	assert.equal(withSource['move-to-1'], undefined);
	assert.equal(withSource['double-from-1'], undefined);
	const playing = byId(
		deckMenu({
			loaded: true,
			playing: true,
			looping: false,
			synced: false,
			master: false,
			selection: false,
			keyLock: false,
			quantize: false,
			slip: false,
			otherLoaded: [2]
		})
	);
	assert.equal(playing['double-from-2'].enabled, false);
	assert.match(playing['double-from-2'].disabledReason, /playing/);
});

test('#9448: Lock beatgrid is shown but always disabled with a reason', () => {
	for (const row of permutations.pairwiseContexts('deck').rows) {
		const item = deckMenu(row).find((i) => i.id === 'grid-lock');
		assert.ok(item, 'grid-lock present');
		assert.equal(item.enabled, false);
		assert.ok((item.disabledReason ?? '').length > 0);
	}
});

test('#9448: key lock, quantize and slip are check items that stay enabled on an empty deck', () => {
	const items = byId(deckMenu(DECK_CTX_EMPTY_SELECTION));
	for (const id of ['key-lock', 'quantize', 'slip']) {
		assert.equal(items[id].enabled, true, id);
		assert.equal(typeof items[id].checked, 'boolean');
	}
});

// Stand-in until #9447 lands reference.json and context-menu-coverage.test.mjs.
const EPIC_DECK_REF = [
	['Eject', 'unload'],
	['Instant double', 'double-from-2'],
	['Find in library', 'find-in-library'],
	['Track info', 'track-info'],
	['Key lock', 'key-lock'],
	['Quantize', 'quantize'],
	['Slip', 'slip'],
	['Grid lock', 'grid-lock'],
	['Sync', 'beat-sync'],
	['Master', 'make-master']
];

test('PRRT_kwDOSEvNd860wjZw: deck Load selected track is one plain load, never an unload or replace_playing', () => {
	const types = performanceCommandTypes();
	const cmd = deckMenuCommands.deckLoadSelectionCommand(1, 'trk');
	assert.deepEqual(cmd, { type: 'load', deck: 1, stable_id: 'trk' });
	assert.equal('replace_playing' in cmd, false);
	assert.equal(deckMenuCommands.deckLoadSelectionCommand(1, null), null);
	assert.ok(types.has(cmd.type));
});

test('PRRT_kwDOSEvNd860v-ho: move labels carry the replaces-playing warning', () => {
	const view = (d) => {
		if (d === 2) return { stable_id: 'trk2', title: 'Song', playing: true };
		if (d === 3) return { stable_id: null, title: null, playing: false };
		return { stable_id: null, title: null, playing: false };
	};
	const entries = deckMove.deckMoveMenuEntries(1, [1, 2, 3, 4], view);
	const labels = deckMenuCommands.deckMoveLabels(entries);
	const loadedCtx = {
		loaded: true,
		playing: false,
		looping: false,
		synced: false,
		master: false,
		selection: false,
		keyLock: false,
		quantize: false,
		slip: false,
		otherLoaded: [2]
	};
	const base = deckMenu(loadedCtx);
	const playBefore = base.find((i) => i.id === 'play').label;
	const relabeled = deckMenuCommands.applyDeckMenuLabels(base, labels);
	const byItem = Object.fromEntries(relabeled.map((i) => [i.id, i]));
	assert.equal(byItem['move-to-2'].label, 'Move track to deck 2 (replaces Song, playing)');
	assert.equal(byItem['move-to-3'].label, 'Move track to deck 3');
	assert.equal(byItem.play.label, playBefore);
	const unchanged = deckMenuCommands.applyDeckMenuLabels(base, new Map());
	for (const item of unchanged) {
		assert.equal(item.label, base.find((i) => i.id === item.id).label);
	}
});

test('PRRT_kwDOSEvNd860v-hu: deck Track info names its deck', () => {
	const types = performanceCommandTypes();
	const cmd = deckMenuCommands.deckTrackInfoCommand(3);
	assert.deepEqual(cmd, { type: 'track_info_open', deck: 3 });
	assert.ok(types.has('track_info_open'));
});

test("#9448: deck covers at least 80% of the epic's rekordbox / Serato deck reference", () => {
	const ctx = {
		loaded: true,
		playing: false,
		looping: false,
		synced: false,
		master: false,
		selection: false,
		keyLock: false,
		quantize: false,
		slip: false,
		otherLoaded: [2]
	};
	const ids = new Set(deckMenu(ctx).map((i) => i.id));
	let present = 0;
	for (const [, itemId] of EPIC_DECK_REF) {
		if (ids.has(itemId)) present += 1;
	}
	assert.ok(present / EPIC_DECK_REF.length >= 0.8, `${present}/${EPIC_DECK_REF.length}`);
});
