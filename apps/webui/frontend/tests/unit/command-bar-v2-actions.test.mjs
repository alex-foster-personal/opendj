// requirement: CMDK-11
// requirement: CMDK-12
// requirement: CMDK-13
// requirement: CMDK-14
// requirement: CMDK-15
// requirement: CMDK-16
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Cmd K slice 3 (#6091): settings and commands as results, the one-character
 * rule, the quick pick, the eject guard, `cmdk_open` / `cmdk_close` through the
 * dispatcher, and the pick that waits for its deck's queued commands.
 *
 * Settings come from the real catalog and its search, so these tests also
 * catch a label or option drifting away from what the bar offers.
 *
 * Regression lines:
 * - if one typed character searches tracks then broken
 * - if two characters do not search tracks then broken
 * - if "gothic" does not offer the skin setting, or Enter does not switch to Gothic, then broken
 * - if punctuation alone ("?") lists every setting, so Enter flips one nobody named, then broken
 * - if a row that only mentions skins ranks above the skin row for "gothic" then broken
 * - if a setting row carries its own copy of a catalog label then broken
 * - if ejecting a playing deck runs without asking then broken
 * - if an idle deck's eject asks anyway then broken
 * - if Ctrl+digit is a quick pick on macOS, or Cmd+digit off it, then broken
 * - if Alt+digit (the deck pick) or Shift+digit is read as a quick pick then broken
 * - if cmdk_open does not reach the mounted bar, or works with none mounted, then broken
 * - if a pick loads while an IPC load for its deck is still queued then broken
 * - if a pick never loads once its deck is idle again then broken
 */

const SRC = new URL('../../src/', import.meta.url).pathname;
let actions;
let search;
let catalog;
let load;
let ipc;

before(async () => {
	actions = await loadTypeScriptModule('src/lib/rb/cmdk-actions.ts');
	search = await loadTypeScriptModule('src/lib/settings/search.ts');
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	load = await loadTypeScriptModule('src/lib/rb/cmdk-load.ts');
	ipc = await loadTypeScriptModule('tests/unit/fixtures/cmdk-ipc-entry.ts');
});

const DECKS = [
	{ id: 1, loaded: true, playing: true, title: 'Copper Lantern', load_generation: 3 },
	{ id: 2, loaded: true, playing: false, title: 'Night Ferry', load_generation: 7 },
	{ id: 3, loaded: false, playing: false, title: null, load_generation: 0 },
	{ id: 4, loaded: false, playing: false, title: null, load_generation: 0 }
];
const TRACK = (id) => ({ stable_id: id });

/** What the bar builds from the overlay's own search, with every value read as given. */
function settingRows(query, values = {}) {
	const found = search.filterSettings(query, { hideTodo: true, showDev: false, group: null }).keyword;
	return found.filter(actions.actionableSetting).map((def) => ({
		kind: 'setting',
		id: def.id,
		label: def.label,
		path: `Settings > ${catalog.groupLabel(def.group)}`,
		def,
		value: values[def.id] ?? (def.control.kind === 'boolean' ? false : def.control.kind === 'enum' ? def.control.options[0].value : '')
	}));
}

function layout(query, extra = {}) {
	return actions.layoutGroups({
		query,
		recent: [TRACK('recent-1')],
		playlist: { rows: [TRACK('pl-1')], total: 1 },
		collection: { rows: [TRACK('col-1')], total: 1 },
		commands: actions.commandCandidates(DECKS, query),
		settings: settingRows(query),
		...extra
	});
}

test('CMDK-11: one character searches only commands, settings and recent loads; two bring tracks', () => {
	assert.equal(actions.tracksAllowed('g'), false);
	assert.equal(actions.tracksAllowed(' g '), false);
	assert.equal(actions.tracksAllowed('g o'), true);
	assert.equal(actions.tracksAllowed('go'), true);
	// One emoji is one character, though it is two UTF-16 units.
	assert.equal(actions.tracksAllowed('\u{1F600}'), false);
	assert.equal(actions.tracksAllowed('\u{1F600}a'), true);
	// A decomposed accent and a ZWJ family are each one character the user sees (Sol P2, #6691).
	assert.equal(actions.queryChars('e\u0301'), 1);
	assert.equal(actions.tracksAllowed('e\u0301'), false);
	assert.equal(actions.queryChars('\u{1F468}\u200D\u{1F469}\u200D\u{1F467}'), 1);
	assert.equal(actions.tracksAllowed('\u{1F468}\u200D\u{1F469}\u200D\u{1F467}'), false);

	const one = layout('g');
	assert.deepEqual(
		one.groups.map((g) => g.id),
		['actions', 'recent']
	);
	assert.equal(one.keepTyping, true);
	assert.ok(one.groups[0].entries.every((e) => e.kind === 'action'));
	assert.ok(one.groups[0].entries.length <= actions.ACTION_LIMIT);

	const two = layout('go');
	assert.deepEqual(
		two.groups.map((g) => g.id).slice(0, 2),
		['playlist', 'collection']
	);
	assert.equal(two.keepTyping, false);
	assert.ok(!two.groups.some((g) => g.id === 'recent'), 'recent loads join only the one-character list');

	const none = layout('');
	assert.deepEqual(
		none.groups.map((g) => g.id),
		['recent', 'commands', 'playlist']
	);
	assert.equal(none.groups[1].entries.length, actions.COMMANDS_ON_OPEN);
});

test('a boolean row holds a real boolean; any other stored value is a fault, not Off (Sol P1, #6689)', () => {
	const flag = { id: 'flag', label: 'Flag', control: { kind: 'boolean' } };
	assert.equal(actions.settingValueLabel(flag, true), 'On');
	assert.equal(actions.settingValueLabel(flag, false), 'Off');
	assert.equal(actions.settingValueLabel(flag, 'false'), 'false (not a boolean)', 'a malformed string is not shown as Off');
	assert.equal(actions.settingVerb(flag, 'false', []), 'Not a boolean; fix it in Settings');
	assert.throws(() => actions.settingNextValue(flag, 'false', []), /not a boolean/, 'Enter refuses it rather than writing true');
	assert.equal(actions.settingNextValue(flag, true, []), false);
});

test('CMDK-12: "gothic" offers the skin setting and Enter switches to Gothic, using the catalog labels', () => {
	const rows = settingRows('gothic', { ui_skin: 'default' });
	const skin = rows.find((row) => row.id === 'ui_skin');
	assert.ok(skin, 'the skin setting is offered for "gothic"');
	const def = catalog.SETTINGS_CATALOG.find((d) => d.id === 'ui_skin');
	assert.equal(skin.label, def.label, 'the row shows the catalog label, not a copy');
	assert.equal(actions.settingNextValue(skin.def, 'default', ['gothic']), 'mono-dev');
	assert.equal(actions.settingVerb(skin.def, 'default', ['gothic']), 'Enter for Gothic (mono dev preview)');
	assert.equal(actions.settingValueLabel(skin.def, 'default'), 'Default');
	// Already Gothic: Enter moves on rather than writing the same value again.
	assert.equal(actions.settingNextValue(skin.def, 'mono-dev', ['gothic']), 'light');
	// No option named: the next one in order, wrapping.
	assert.equal(actions.enumTarget(skin.def, 'light', ['skin']), 'default');
	assert.throws(() => actions.enumTarget({ ...def, control: { kind: 'boolean' } }, true, []), /not an enum/);
});

test('CMDK-12: Enter reaches the row whose option the query names before rows that only mention it', () => {
	const order = (skin) => actions.rankSettings(settingRows('gothic', { ui_skin: skin }), 'gothic').map((row) => row.id);
	const raw = settingRows('gothic', { ui_skin: 'default' }).map((row) => row.id);
	// The control: the catalog's own order puts a skin-mentioning row ahead of the skin.
	assert.ok(raw.indexOf('waveform_design') !== -1 && raw.indexOf('waveform_design') < raw.indexOf('ui_skin'), raw.join());
	assert.equal(order('default')[0], 'ui_skin', 'Gothic is one Enter away when it is not already set');
	// Already Gothic: the row showing Gothic still leads the rows that only mention skins.
	assert.equal(order('mono-dev')[0], 'ui_skin');
	// Stable: rows in the same tier keep catalog order.
	const rest = order('default').slice(1);
	assert.deepEqual(rest, raw.filter((id) => id !== 'ui_skin'));
	// A row that would switch to the named option outranks one whose own label names it.
	const skinDef = catalog.SETTINGS_CATALOG.find((d) => d.id === 'ui_skin');
	const named = { kind: 'setting', id: 'named-label', label: 'Gothic tint', path: '', def: { ...skinDef, id: 'named-label', control: { kind: 'boolean' } }, value: false };
	const skin = { kind: 'setting', id: 'ui_skin', label: skinDef.label, path: '', def: skinDef, value: 'default' };
	assert.deepEqual(actions.rankSettings([named, skin], 'gothic').map((row) => row.id), ['ui_skin', 'named-label']);
	assert.deepEqual(actions.rankSettings([skin, named], 'zzz').map((row) => row.id), ['ui_skin', 'named-label']);
});

test('CMDK-12: punctuation alone offers no setting, so Enter cannot flip one nobody named', () => {
	const opts = { hideTodo: false, showDev: false, group: null };
	// The hazard: the settings search lists the whole pool for a query it cannot tokenize.
	assert.ok(search.filterSettings('?', opts).keyword.length > 1, 'filterSettings itself lists everything for "?"');
	for (const q of ['?', '!!', '#', ' / ']) assert.equal(search.hasSettingsQuery(q), false, `"${q}" is no settings query`);
	// Control: a real word, and a word inside punctuation, still search.
	for (const q of ['gothic', 'auto?', 'beat-sync']) assert.equal(search.hasSettingsQuery(q), true, `"${q}" searches settings`);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const derived = bar.slice(bar.indexOf('const settingActions = $derived.by('), bar.indexOf('const found = filterSettings(query'));
	assert.match(derived, /!hasSettingsQuery\(query\)\) return \[\];/);
});

test('CMDK-12: booleans flip, links open, and unbuilt or slider rows are not offered', () => {
	const autoplay = catalog.SETTINGS_CATALOG.find((d) => d.id === 'auto_play_enabled');
	assert.equal(actions.settingNextValue(autoplay, false, []), true);
	assert.equal(actions.settingNextValue(autoplay, true, []), false);
	assert.equal(actions.settingVerb(autoplay, true, []), 'Enter to toggle');
	assert.equal(actions.settingValueLabel(autoplay, true), 'On');
	const link = catalog.SETTINGS_CATALOG.find((d) => d.control.kind === 'link' && d.implemented);
	if (link !== undefined) {
		assert.equal(actions.settingNextValue(link, '', []), null);
		assert.equal(actions.actionableSetting(link), true);
	}
	for (const def of catalog.SETTINGS_CATALOG) {
		if (!def.implemented || def.control.kind === 'number' || def.control.kind === 'path_lines' || def.control.kind === 'multi_bool') {
			assert.equal(actions.actionableSetting(def), false, `${def.id} is not offered`);
		}
	}
});

test('CMDK-12: the bar renders the hint only on the highlighted or hovered row', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /\.act \.ent \{[^}]*visibility: hidden;/);
	assert.match(bar, /\.act\.selected \.ent,\s*\.act:hover \.ent \{\s*visibility: visible;/);
	// The bar never imports prefs.svelte; the settings module comes in lazily on first open.
	assert.match(bar, /import\('\$lib\/settings\/apply'\)/);
	assert.doesNotMatch(bar, /from '\$lib\/settings\/apply'/);
	// The settings the bar offers go through the ranking, not catalog order alone.
	assert.match(bar, /return rankSettings\(out, query\);/);
	// The dispatcher reaches the bar through the page, so the bar adds no importer to it.
	assert.doesNotMatch(bar, /from '\$lib\/rb\/performance-ipc\.svelte'/);
	const page = readFileSync(`${SRC}routes/performance/+page.svelte`, 'utf8');
	assert.match(
		page,
		/runCommand=\{\(command, source\) => runPerformanceCommandFromUi\(command, performance\.now\(\), undefined, source\)\}\s*runCoordinated=\{runCoordinatedFromUi\}\s*deckLoadDispatches=\{deckLoadDispatchCount\}\s*faderWrites=\{deckFaderWriteCount\}\s*deckCommands=\{deckCommandDispatchCount\}\s*deckPending=\{\(deck\) => performanceCommandStatus\.deck_pending\[deck\]\}\s*registerAdapter=\{registerCmdkAdapter\}/
	);
});

test('CMDK-14: an eject is offered per loaded deck, and a playing one asks first', () => {
	const cmds = actions.commandCandidates(DECKS, 'ej');
	assert.deepEqual(
		cmds.map((c) => c.id),
		['eject-1:3', 'eject-2:7', 'open-settings']
	);
	// An eject row read before deck 2 was reloaded must not match the row after it (Codex P2 BLOCKING, #6691).
	const reloaded = actions.commandCandidates(
		DECKS.map((d) => (d.id === 2 ? { ...d, load_generation: 8 } : d)),
		'ej'
	);
	assert.notEqual(reloaded[1].id, cmds[1].id);
	assert.match(cmds[0].hint, /playing, asks first/);
	assert.doesNotMatch(cmds[1].hint, /asks first/);
	assert.equal(actions.ejectAction({ playing: true }, false), 'ask');
	assert.equal(actions.ejectAction({ playing: true }, true), 'run');
	assert.equal(actions.ejectAction({ playing: false }, false), 'run');
	const rows = actions.actionRows('eject', cmds, []);
	assert.deepEqual(
		rows.map((r) => r.id),
		['eject-1:3', 'eject-2:7']
	);
	assert.deepEqual(actions.actionRows('', cmds, []), []);
	assert.equal(actions.commandCandidates(DECKS, 'skin').at(-1).label, 'Open settings for "skin"');
});

test('CMDK-13: quick pick is Cmd+1-9 on macOS and Ctrl+1-9 elsewhere, and nothing else', () => {
	assert.equal(actions.quickPickModifier('MacIntel'), 'Meta');
	assert.equal(actions.quickPickModifier('Win32'), 'Control');
	assert.equal(actions.quickPickModifier('Linux x86_64'), 'Control');
	const key = (code, mods = {}) => ({ code, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods });
	assert.equal(actions.quickPickIndex(key('Digit1', { metaKey: true }), 'Meta'), 0);
	assert.equal(actions.quickPickIndex(key('Digit9', { metaKey: true }), 'Meta'), 8);
	assert.equal(actions.quickPickIndex(key('Digit0', { metaKey: true }), 'Meta'), null);
	assert.equal(actions.quickPickIndex(key('Digit3', { ctrlKey: true }), 'Meta'), null);
	assert.equal(actions.quickPickIndex(key('Digit3', { ctrlKey: true }), 'Control'), 2);
	assert.equal(actions.quickPickIndex(key('Digit3', { metaKey: true }), 'Control'), null);
	assert.equal(actions.quickPickIndex(key('Digit3', { metaKey: true, altKey: true }), 'Meta'), null);
	assert.equal(actions.quickPickIndex(key('Digit3', { metaKey: true, shiftKey: true }), 'Meta'), null);
	assert.equal(actions.quickPickIndex(key('KeyK', { metaKey: true }), 'Meta'), null);
	assert.equal(actions.quickPickIndex(key('Digit3'), 'Meta'), null);
	// Opened with the modifier still held, the numbers show from the opening chord.
	assert.equal(actions.quickModifierHeld(key('KeyK', { metaKey: true }), 'Meta'), true);
	assert.equal(actions.quickModifierHeld(key('KeyK', { ctrlKey: true }), 'Meta'), false);
	assert.equal(actions.quickModifierHeld(key('KeyK', { ctrlKey: true }), 'Control'), true);
	assert.equal(actions.quickModifierHeld(key('KeyK', { metaKey: true }), 'Control'), false);
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	assert.match(bar, /quickHeld = actionsOn && quickModifierHeld\(event, quickMod\);\n\t+void openBar\(\);/);
});

test('CMDK-11: recent loads keep the newest first, once each, at most five', () => {
	let recent = [];
	for (const id of ['a', 'b', 'c', 'a', 'd', 'e', 'f']) recent = actions.noteRecentLoad(recent, id);
	assert.deepEqual(recent, ['f', 'e', 'd', 'a', 'c']);
	// Loading the same track again moves it to the top instead of listing it twice.
	assert.deepEqual(actions.noteRecentLoad(['b', 'a'], 'a'), ['a', 'b']);
});

test('CMDK-15: cmdk_open and cmdk_close reach the mounted bar and its state reads back', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_open' }), /requires the Cmd K v2 bar/);
		assert.equal(ipc.queryPerformanceState().cmdk, null);
		const bar = { open: false, query: '', selected: 0, rows: [] };
		const unregister = ipc.registerCmdkAdapter({
			open: async (text) => {
				bar.open = true;
				if (text !== null) bar.query = text;
				bar.rows = [{ kind: 'setting', id: 'ui_skin', label: 'UI skin' }];
			},
			close: () => {
				bar.open = false;
			},
			snapshot: () => ({ ...bar, rows: [...bar.rows] })
		});
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_open', query: 'gothic' });
		assert.deepEqual(ipc.queryPerformanceState().cmdk, {
			open: true,
			query: 'gothic',
			selected: 0,
			rows: [{ kind: 'setting', id: 'ui_skin', label: 'UI skin' }]
		});
		await ipc.dispatchPerformanceCommand({ type: 'cmdk_close' });
		assert.equal(ipc.queryPerformanceState().cmdk.open, false);
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_open', query: 7 }), /query must be a string/);
		await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'cmdk_close', query: 'x' }), /unexpected fields/);
		unregister();
		assert.equal(ipc.queryPerformanceState().cmdk, null);
	} finally {
		uninstall();
	}
});

test('CMDK-16: a load dispatched during the wait holds the pick until it settles, and wins when it commits', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: null, file_availability: 'AVAILABILITY_PENDING' };
	const make = (state) =>
		load.createPickLoader(
			async () => {
				// The rb-meta wait: an IPC load for the same deck is dispatched now.
				state.dispatched += state.dispatchDuringWait;
				state.pending = state.dispatchDuringWait;
				return { file_exists: true, is_streaming: false };
			},
			async (picked, deck) => {
				state.loaded.push([picked.stable_id, deck]);
				return true;
			},
			() => state.mark,
			{
				loadsDispatched: () => state.dispatched,
				commandsDispatched: () => 0,
				pending: () => 0,
				idle: () =>
					load.waitDeckIdle(
						() => state.pending,
						async () => {
							state.checks += 1;
							// The queued load settles on the second check.
							if (state.checks === 2) {
								state.pending = 0;
								if (state.commits) state.mark = 'ipc|2|0';
							}
						},
						1000,
						10
					)
			}
		);

	const commits = { dispatched: 0, dispatchDuringWait: 1, pending: 0, checks: 0, mark: 'old|1|0', commits: true, loaded: [] };
	assert.equal(await make(commits)(row, 1), false);
	assert.deepEqual(commits.loaded, [], 'the newer IPC load stays');
	assert.equal(commits.checks, 2, 'the pick waited for the queue');

	const fails = { dispatched: 0, dispatchDuringWait: 1, pending: 0, checks: 0, mark: 'old|1|0', commits: false, loaded: [] };
	assert.equal(await make(fails)(row, 1), true);
	assert.deepEqual(fails.loaded, [['cmdk-pick', 1]], 'a queued load that failed does not cancel the pick');

	// No load dispatched during the wait: older commands still queued are drained, and an older
	// load that commits there does not cancel the pick (it was dispatched before the pick).
	const older = { dispatched: 3, pending: 2, waited: 0, mark: 'old|1|0|paused', loaded: [] };
	const olderPort = (state, settle) => ({
		loadsDispatched: () => state.dispatched,
		commandsDispatched: () => 0,
		pending: () => state.pending,
		idle: async () => {
			state.waited += 1;
			state.pending = 0;
			settle(state);
			return true;
		}
	});
	const loadInto = (state) => async (picked, deck) => {
		state.loaded.push([picked.stable_id, deck]);
		return true;
	};
	const pick = load.createPickLoader(async () => ({ file_exists: true, is_streaming: false }), loadInto(older), () => older.mark,
		olderPort(older, (state) => { state.mark = 'older-load|2|0|paused'; }));
	assert.equal(await pick(row, 2), true);
	assert.equal(older.waited, 1, 'the queue was drained first');
	assert.deepEqual(older.loaded, [['cmdk-pick', 2]]);

	// A play from MIDI or IPC queued on the paused deck during the wait (not a load, so the load
	// count stays): once it lands the deck plays, and the unconfirmed pick stands down (Codex r4276598137).
	const play = { dispatched: 3, pending: 1, waited: 0, mark: 'old|1|0|paused', loaded: [] };
	const playPick = load.createPickLoader(async () => ({ file_exists: true, is_streaming: false }), loadInto(play), () => play.mark,
		olderPort(play, (state) => { state.mark = 'old|1|0|playing'; }));
	assert.equal(await playPick(row, 2), false);
	assert.equal(play.waited, 1);
	assert.deepEqual(play.loaded, [], 'a deck that started playing is never replaced without confirmation');
	// A load sent while the drain runs is newer than the pick and wins it, even though it lands paused
	// (Codex P1 BLOCKING, #6689): the pick stands down and loads nothing.
	// The first drain only waits (the deck stays queued); the second, after the pick is known to be
	// the newest, is where the load lands.
	const newerDuringDrain = { dispatched: 3, pending: 1, waited: 0, mark: 'old|1|0|paused', loaded: [] };
	const drainPick = load.createPickLoader(async () => ({ file_exists: true, is_streaming: false }), loadInto(newerDuringDrain), () => newerDuringDrain.mark,
		{
			loadsDispatched: () => newerDuringDrain.dispatched,
			commandsDispatched: () => 0,
			pending: () => newerDuringDrain.pending,
			idle: async () => {
				newerDuringDrain.waited += 1;
				if (newerDuringDrain.waited === 2) {
					newerDuringDrain.dispatched += 1;
					newerDuringDrain.pending = 0;
				}
				return true;
			}
		});
	assert.equal(await drainPick(row, 2), false);
	assert.deepEqual(newerDuringDrain.loaded, [], 'a load that lands during the drain is not replaced by the pick');
	// Control: nothing queued, nothing waited.
	const idle = { dispatched: 3, pending: 0, waited: 0, mark: 'old|1|0|paused', loaded: [] };
	const idlePick = load.createPickLoader(async () => ({ file_exists: true, is_streaming: false }), loadInto(idle), () => idle.mark,
		olderPort(idle, () => assert.fail('an idle deck is not waited on')));
	assert.equal(await idlePick(row, 2), true);
	assert.equal(idle.waited, 0);
	assert.equal(load.startedPlaying('a|1|0|paused', 'a|1|0|playing'), true);
	assert.equal(load.startedPlaying('a|1|0|playing', 'a|1|0|playing'), false);
	assert.equal(load.startedPlaying('a|1|0|paused', 'b|2|0|paused'), false);
});

test('CMDK-16: the dispatcher counts every load and unload it is sent, per deck, before any commits', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const before = [1, 2, 3, 4].map((d) => ipc.deckLoadDispatchCount(d));
		// Refused for a missing track, but it was dispatched: the count is about dispatch, not commit.
		await ipc.dispatchPerformanceCommand({ type: 'unload', deck: 2 }).catch(() => {});
		await ipc.dispatchPerformanceCommand({ type: 'load', deck: 2, stable_id: 'no-such-track' }).catch(() => {});
		assert.equal(ipc.deckLoadDispatchCount(2), before[1] + 2);
		assert.equal(ipc.deckLoadDispatchCount(1), before[0], 'another deck does not move');
		await ipc.dispatchPerformanceCommand({ type: 'play', deck: 2, playing: false }).catch(() => {});
		assert.equal(ipc.deckLoadDispatchCount(2), before[1] + 2, 'a deck command that is not a load does not move it');
	} finally {
		uninstall();
	}
});

test('CMDK-16: a deck that never goes idle drops the pick instead of racing it', async () => {
	// Each sleep costs 40 ms. A count of requested sleeps would run for ~400 ms; the wall-clock bound stops near 120 ms.
	const started = performance.now();
	const idle = await load.waitDeckIdle(
		() => 1,
		() => new Promise((resolve) => setTimeout(resolve, 40)),
		100,
		10
	);
	const elapsed = performance.now() - started;
	assert.equal(idle, false);
	assert.ok(elapsed >= 100, `gave up after ${elapsed} ms, before its 100 ms bound`);
	assert.ok(elapsed < 300, `took ${elapsed} ms: the bound must be wall-clock time, not a count of sleeps`);
	assert.equal(await load.waitDeckIdle(() => 0, async () => assert.fail('no sleep when idle'), 100, 10), true);
});

test('Sol P1, #6689: a command that settles during a sleep that runs past the deadline is a timeout, not an idle queue', async () => {
	// The clock jumps 150 ms in one clamped sleep, past the 100 ms bound, and the last command settles during it.
	let clock = 0;
	let pending = 1;
	const idle = await load.waitDeckIdle(
		() => pending,
		async () => {
			clock += 150;
			pending = 0;
		},
		100,
		10,
		() => clock
	);
	assert.equal(idle, false);
});

test('a deck mark needs a boolean play state, so a missing one fails instead of reading as paused', () => {
	const deck = { stable_id: 'a', load_generation: 1, playing: false };
	assert.match(load.deckLoadMark(deck, 0), /\|paused$/);
	assert.throws(() => load.deckLoadMark({ ...deck, playing: undefined }, 0), /no boolean playing field/);
});

test('an eject confirm answers only the load it asked about (Codex r4275059846)', () => {
	const src = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	assert.match(src, /let ejectGuard = \$state<ReplaceGuard \| null>\(null\);/);
	assert.match(src, /ejectGuard = replaceGuard\(command\.deck, decks\[command\.deck\], deckLoadDispatches\(command\.deck\), deckCommands\(command\.deck\)\);/);
	const confirm = src.slice(src.indexOf('async function confirmEject'), src.indexOf('const GROUP_NAME'));
	// A moved deck is asked about again, playing or not (Codex r4275496748); only a held guard ejects.
	// A load dispatched while the confirm was open counts as moved too, and the question is asked
	// again only once it has landed (Codex r4277027005).
	assert.match(confirm, /const commandsAt = ejectCommands;\s*if \(!guardHolds\(guard, decks\[guard\.id\], deckLoadDispatches\(guard\.id\), deckCommands\(guard\.id\)\) \|\| deckCommands\(guard\.id\) !== commandsAt\) \{\s*const session = barSession;\s*const again = await settledGuard\(guard\.id, session\);\s*(?:\/\/[^\n]*\s*)*if \(op !== ejectOp \|\| ejectGuard !== null \|\| barSession !== session\) return;/);
	assert.doesNotMatch(confirm, /runAction\(action, guardHolds/);
});

test('the eject a confirm runs is looked up after the queue drains, not before (Sol, #6689)', () => {
	const src = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const confirm = src.slice(src.indexOf('async function confirmEject'), src.indexOf('const GROUP_NAME'));
	// A lookup taken before the drain misses an eject the deck offers once its replacement lands,
	// and the second confirmation was then silently dropped. Control: the drain comes before the re-check.
	// The first lookup of the eject must come after the drain: a copy taken earlier is the defect.
	assert.ok(confirm.indexOf('await settledGuard') < confirm.indexOf('commandActions.find'));
});

test('plain Enter on a focused "Eject it" button keeps the deck playing (Codex r4275262670)', () => {
	const src = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const wrap = src.slice(src.indexOf('function onWrapKeydown'), src.indexOf('async function confirmEject'));
	// Focus on the guard's buttons is off the input, so the wrapper sees the key: it must stop
	// the native click and confirm only with the modifier.
	assert.match(
		wrap,
		/else if \(ejectGuard !== null && \(event\.key === 'Enter' \|\| event\.key === ' '\)\) \{[\s\S]*?event\.preventDefault\(\);\s*if \(event\.key === 'Enter' && quickModifierHeld\(event, quickMod\)\) void confirmEject\(\);\s*else ejectGuard = null;/
	);
	// Control: the input's own handler keeps the same rule.
	assert.match(src, /if \(event\.key === 'Enter' && quickModifierHeld\(event, quickMod\)\) void confirmEject\(\);/);
});

test('CMDK-19: a pick started before its prelude counts a load dispatched during the prelude as newer (Codex r4275268147)', async () => {
	const row = { stable_id: 'cmdk-pair', file_exists: true, file_availability: 'present' };
	const make = (state) =>
		load.createPickLoader(
			async () => assert.fail('a settled row asks rb-meta nothing'),
			async (picked, deck) => {
				state.loaded.push([picked.stable_id, deck]);
				return true;
			},
			() => state.mark,
			{
				loadsDispatched: () => state.dispatched,
				commandsDispatched: () => 0,
				pending: () => 0,
				idle: async () => {
					state.waited = true;
					// The rival load queued during the prelude commits.
					state.mark = 'rival|2|0';
					return true;
				}
			}
		);
	const early = { dispatched: 0, mark: 'old|1|0', loaded: [], waited: false };
	const pick = make(early);
	const start = pick.begin(3);
	// The fader prelude runs; an IPC load for deck 3 is dispatched meanwhile.
	early.dispatched += 1;
	assert.equal(await pick(row, 3, start), false);
	assert.equal(early.waited, true, 'the pick waited for the newer load');
	assert.deepEqual(early.loaded, [], 'the newer IPC load stays');
	// Control: a pick that starts only after that dispatch takes it for an older load and goes ahead.
	const late = { dispatched: 0, mark: 'old|1|0', loaded: [], waited: false };
	const after = make(late);
	late.dispatched += 1;
	assert.equal(await after(row, 3), true);
	assert.equal(late.waited, false);
	assert.deepEqual(late.loaded, [['cmdk-pair', 3]]);
	// The aligned load starts its pick before the fader moves and hands it to the load.
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const aligned = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const begun = aligned.indexOf('let start = pickLoad.begin(deck);');
	assert.ok(begun > 0 && begun < aligned.indexOf('beforeAlignedLoad(deck)'), 'begun before the prelude');
	assert.match(aligned, /await pickLoad\(settledRow, deck, start\)/);
});

test('CMDK-16: a load sent while the replacement unload runs keeps the deck; the pick asks again right before its own load (Codex r4275568175)', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: true, file_availability: 'present' };
	const state = { dispatched: 0, mark: 'old|1|0' };
	const sent = [];
	// The load path as BrowserPanel runs it: the deck holds a track, so it unloads first
	// (one dispatch of its own), then asks the guard right before it sends the load.
	const loadOnto = async (picked, deck, stillNewest) => {
		state.dispatched += 1; // its own unload
		sent.push('unload');
		if (state.rival) {
			state.dispatched += 1; // an IPC load queued behind that unload
			sent.push('rival load');
		}
		if (!stillNewest(1)) return false;
		state.dispatched += 1;
		sent.push(`load ${picked.stable_id}`);
		return true;
	};
	const pick = load.createPickLoader(async () => assert.fail('a settled row asks rb-meta nothing'), loadOnto, () => state.mark, {
		loadsDispatched: () => state.dispatched,
		commandsDispatched: () => 0,
		pending: () => 0,
		idle: async () => true
	});
	// Control: nothing else is sent, the pick's own unload is not mistaken for a rival.
	assert.equal(await pick(row, 2), true);
	assert.deepEqual(sent, ['unload', 'load cmdk-pick']);
	// A rival load lands during the unload: the pick stands down and never overwrites it.
	sent.length = 0;
	state.rival = true;
	assert.equal(await pick(row, 2), false);
	assert.deepEqual(sent, ['unload', 'rival load']);
	// BrowserPanel counts its own unload and asks right before the load dispatch.
	const panel = readFileSync(`${SRC}lib/components/rb/BrowserPanel.svelte`, 'utf8');
	const body = panel.slice(panel.indexOf('async function _loadOntoDeck'), panel.indexOf('export async function commandBarV2Load'));
	const ownUnload = body.indexOf('own += 1;');
	const unload = body.indexOf("await dispatchPerformanceCommand({ type: 'unload', deck: target, refuseIfMaster: true });");
	const guard = body.lastIndexOf('if (opts.stillNewest !== undefined && !opts.stillNewest(own)) {');
	const loadSend = body.indexOf("type: 'load',");
	assert.ok(ownUnload > 0 && ownUnload < unload && unload < guard && guard < loadSend, 'own unload counted, then the guard, then the load');
	// And once before the unload, so a load that landed during the intent dispatch is never the
	// track the replacement removes (Codex r4276791443).
	const early = body.indexOf('if (opts.stillNewest !== undefined && !opts.stillNewest(own)) {');
	const intent = body.indexOf("type: 'load_play_intent',");
	assert.ok(intent > 0 && intent < early && early < ownUnload && early !== guard, 'the guard also runs between the intent and the unload');
});

test('CMDK-14: an unconfirmed eject waits out the deck queue before reading playing (Codex r4276912170)', () => {
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const body = bar.slice(bar.indexOf('async function runAction'), bar.indexOf('function activate('));
	const wait = body.indexOf('if (deckPending(command.deck) > 0) {');
	const ask = body.indexOf("if (ejectAction(decks[command.deck], confirmed) === 'ask') {");
	const send = body.indexOf("await runCommand({ type: 'unload', deck: command.deck }, source);");
	assert.ok(wait > 0 && wait < ask && ask < send, 'drain, then the playing check, then the unload');
	const gate = body.slice(wait, ask);
	assert.match(gate, /await waitDeckIdle\(\s*\(\) => deckPending\(command\.deck\),/);
	assert.match(gate, /if \(!drained\) return fail\(`Deck \$\{command\.deck\} is still busy[^`]*`\);/);
	// No await between the playing check and the unload dispatch: nothing can be queued in between.
	assert.doesNotMatch(body.slice(ask, send), /await /);
});

test('CMDK-14: a confirmed eject drains too, and ejects only the track it was confirmed for (Codex r4278070869)', () => {
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const body = bar.slice(bar.indexOf('async function runAction'), bar.indexOf('function activate('));
	// The confirm is pinned before the drain, and checked after it, before the unload.
	const pin = body.indexOf('const asked = confirmed ? replaceGuard(command.deck, decks[command.deck], deckLoadDispatches(command.deck), deckCommands(command.deck)) : null;');
	const wait = body.indexOf('if (deckPending(command.deck) > 0) {');
	const check = body.indexOf('if (asked !== null && !guardHolds(asked, decks[command.deck], deckLoadDispatches(command.deck), deckCommands(command.deck))) {');
	const send = body.indexOf("await runCommand({ type: 'unload', deck: command.deck }, source);");
	assert.ok(pin > 0 && pin < wait && wait < check && check < send, 'pin, drain, check, then the unload');
	assert.match(body.slice(check), /^if \(asked !== null [^\n]*\{\s*return fail\(`Deck \$\{command\.deck\} took another load before the eject; it was not ejected[^`]*`\);/);
	// The drain is no longer skipped for a confirm.
	assert.doesNotMatch(body, /!confirmed && deckPending\(command\.deck\)/);
	// A settings pick while the module is still loading is refused, not a silent success (Codex r4278070876).
	assert.match(body, /if \(api === null\) return fail\(`\$\{action\.label\} did not change: settings are still loading; pick it again`\);/);
	const deckPicker = readFileSync(new URL('../../src/lib/rb/cmdk-deck-picker.ts', import.meta.url), 'utf8');
	assert.match(deckPicker, /state\.stable_id === guard\.stable_id &&\s*state\.load_generation === guard\.load_generation &&\s*loads === guard\.loads &&\s*commands === guard\.commands/);
});

test('CMDK-16: a play, cue or seek sent to the deck while the pick waits on the load intent keeps the deck (Codex r4277133041)', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: true, file_availability: 'present' };
	const state = { loads: 0, commands: 0, sent: [] };
	// BrowserPanel's order: the load intent (an await anything can slip into), the guard, then the
	// replacement unload, the guard again, and the load.
	const loadOnto = async (picked, deck, stillNewest) => {
		if (state.playDuringIntent) state.commands += 1; // an IPC or MIDI play, queued in the deck scope
		if (!stillNewest(0)) return false;
		state.loads += 1;
		state.sent.push('unload');
		if (!stillNewest(1)) return false;
		state.loads += 1;
		state.sent.push(`load ${picked.stable_id}`);
		return true;
	};
	const pick = load.createPickLoader(async () => assert.fail('a settled row asks rb-meta nothing'), loadOnto, () => 'old|1|0|paused', {
		loadsDispatched: () => state.loads,
		commandsDispatched: () => state.commands,
		pending: () => 0,
		idle: async () => true
	});
	// Control: no one else touches the deck, so the pick replaces it.
	assert.equal(await pick(row, 2), true);
	assert.deepEqual(state.sent, ['unload', 'load cmdk-pick']);
	// A play arrives during the intent: the replacement never reaches the unload.
	state.sent.length = 0;
	state.playDuringIntent = true;
	assert.equal(await pick(row, 2), false);
	assert.deepEqual(state.sent, [], 'the deck the DJ just played is not replaced');
});

test('CMDK-16: a play, cue or seek sent once the replacement unload went out never strands the deck empty (Codex r4278596582)', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: true, file_availability: 'present' };
	const state = { loads: 0, commands: 0, sent: [] };
	const loadOnto = async (picked, deck, stillNewest) => {
		if (!stillNewest(0)) return false;
		state.loads += 1;
		state.sent.push('unload');
		if (state.playDuringUnload) state.commands += 1; // a play queued while the unload ran
		if (state.loadDuringUnload) {
			state.loads += 1;
			state.sent.push('rival load');
		}
		if (!stillNewest(1)) return false;
		state.loads += 1;
		state.sent.push(`load ${picked.stable_id}`);
		return true;
	};
	const pick = load.createPickLoader(async () => assert.fail('a settled row asks rb-meta nothing'), loadOnto, () => 'old|1|0|paused', {
		loadsDispatched: () => state.loads,
		commandsDispatched: () => state.commands,
		pending: () => 0,
		idle: async () => true
	});
	// A play during the unload: the old track is already gone, so the pick's load still goes out.
	state.playDuringUnload = true;
	assert.equal(await pick(row, 2), true);
	assert.deepEqual(state.sent, ['unload', 'load cmdk-pick'], 'the deck is not left empty');
	// Control: a newer load during the unload still wins, so the deck holds that one.
	state.sent.length = 0;
	state.playDuringUnload = false;
	state.loadDuringUnload = true;
	assert.equal(await pick(row, 2), false);
	assert.deepEqual(state.sent, ['unload', 'rival load']);
});

test('CMDK-16: the dispatcher counts deck-queue commands when sent; loads, beat_sync and mixer writes do not move it', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const count = () => [1, 2, 3, 4].map((d) => ipc.deckCommandDispatchCount(d));
		const start = count();
		for (const command of [
			{ type: 'play', deck: 2, playing: true },
			{ type: 'cue', deck: 2 },
			{ type: 'seek', deck: 2, position_ms: 1000 },
			{ type: 'tempo', deck: 2, ratio: 1 }
		]) {
			await ipc.dispatchPerformanceCommand(command).catch(() => {});
		}
		assert.equal(count()[1], start[1] + 4, 'play, cue, seek and tempo each count, whether or not they run');
		assert.equal(count()[0], start[0], 'another deck does not move');
		const mid = count()[1];
		for (const command of [
			{ type: 'beat_sync', deck: 2, enabled: false },
			{ type: 'fader', deck: 2, value: 0 },
			{ type: 'eq', deck: 2, band: 'low', value: 0 },
			{ type: 'unload', deck: 2 },
			{ type: 'load_play_intent', deck: 2, generation: 1, desired_play: false }
		]) {
			await ipc.dispatchPerformanceCommand(command).catch(() => {});
		}
		assert.equal(count()[1], mid, 'beat_sync, mixer writes, unloads and the load intent leave it alone');
	} finally {
		uninstall();
	}
});

test('CMDK-16: a cue or seek sent while the pick waits on rb-meta is newer than the pick, finished or not (Codex r4277285037)', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: null, file_availability: 'AVAILABILITY_PENDING' };
	const state = { commands: 5, loads: 0, sent: [] };
	const pick = load.createPickLoader(
		async () => {
			if (state.cueDuringWait) state.commands += 1; // a MIDI cue, already run by the time rb-meta answers
			return { file_exists: true, is_streaming: false };
		},
		async (picked, deck, stillNewest) => {
			if (!stillNewest(0)) return false;
			state.loads += 1; // the dispatcher counts the load as it is sent
			state.sent.push(`load ${picked.stable_id}`);
			return true;
		},
		() => 'old|1|0|paused',
		{ loadsDispatched: () => state.loads, commandsDispatched: () => state.commands, pending: () => 0, idle: async () => true }
	);
	// Control: commands sent before the pick are older and do not stop it.
	assert.equal(await pick(row, 2), true);
	assert.deepEqual(state.sent, ['load cmdk-pick']);
	state.sent.length = 0;
	state.cueDuringWait = true;
	assert.equal(await pick(row, 2), false);
	assert.deepEqual(state.sent, [], 'the track the DJ just cued is not replaced');
});

test('CMDK-19: a pick begun before its prelude dates deck commands from begin, not from the load (Codex r4277285037)', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: true, file_availability: 'present' };
	const state = { commands: 2, sent: [] };
	const pick = load.createPickLoader(
		async () => assert.fail('a settled row asks rb-meta nothing'),
		async (picked, deck, stillNewest) => {
			if (!stillNewest(0)) return false;
			state.sent.push(picked.stable_id);
			return true;
		},
		() => 'old|1|0|paused',
		// The dispatcher counts each load as it is sent.
		{ loadsDispatched: () => state.sent.length, commandsDispatched: () => state.commands, pending: () => 0, idle: async () => true }
	);
	assert.equal(pick.begin(2).used, 2);
	// Control: nothing sent between begin and the load.
	assert.equal(await pick(row, 2, pick.begin(2)), true);
	// A seek sent during the prelude (after begin) stops it.
	const start = pick.begin(2);
	state.commands += 1;
	assert.equal(await pick(row, 2, start), false);
	assert.deepEqual(state.sent, ['cmdk-pick']);
});

test('CMDK-16: the aligned pick starts before its drain wait; a load or command sent during the wait holds it back (Codex r4277480917)', async () => {
	const state = { loads: 0, commands: 0, mark: 'old|1|0|paused' };
	const pick = load.createPickLoader(
		async () => assert.fail('no rb-meta'),
		async () => true,
		() => state.mark,
		{ loadsDispatched: () => state.loads, commandsDispatched: () => state.commands, pending: () => 0, idle: async () => true }
	);
	// Control: only what was queued before the pick ran during the wait; the pick keeps its place
	// and holds the mark that queued work left.
	const start = pick.begin(2);
	state.mark = 'older|2|0|paused';
	const resumed = pick.resume(2, start);
	assert.notEqual(resumed, null);
	assert.equal(resumed.pick, start.pick);
	assert.equal(resumed.sent, start.sent);
	assert.equal(resumed.used, start.used);
	assert.equal(resumed.mark, 'older|2|0|paused');
	// A rival load dispatched during the wait is newer than the pick.
	const a = pick.begin(2);
	state.loads += 1;
	assert.equal(pick.resume(2, a), null);
	// So is a play, cue or seek sent to the deck.
	const b = pick.begin(2);
	state.commands += 1;
	assert.equal(pick.resume(2, b), null);
	// And a newer pick for the deck.
	const c = pick.begin(2);
	pick.begin(2);
	assert.equal(pick.resume(2, c), null);
	// The bar takes the pick before the drain, resumes it after, and baselines on its count.
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const aligned = bar.slice(bar.indexOf('async function loadAligned'), bar.indexOf('function onWindowKeyup'));
	const begun = aligned.indexOf('let start = pickLoad.begin(deck);');
	const drain = aligned.indexOf('if (deckPending(deck) > 0) {');
	assert.ok(begun > 0 && begun < drain, 'begun before the drain wait');
	assert.match(aligned.slice(drain), /^if \(deckPending\(deck\) > 0\) \{[\s\S]*?const resumed = pickLoad\.resume\(deck, start\);\s*if \(resumed === null\) \{[\s\S]*?return fail\([\s\S]*?\);\s*\}\s*start = resumed;/);
	assert.match(aligned, /let expected = start\.used;/);
});

test('CMDK-03/16: a pick reports only its own load; a rival reload of the same track is never this pick\'s (Sol r4278366181)', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: true, file_availability: 'present' };
	// The load path as BrowserPanel runs it: ask the guard right before the load, stand down on a no,
	// and report "committed" when the deck then holds this stable_id (loadCommitted's rule).
	const make = (state) =>
		load.createPickLoader(
			async () => assert.fail('a settled row asks rb-meta nothing'),
			async (picked, deck, stillNewest) => {
				if (state.rivalFirst) {
					state.loads += 1; // an IPC reload of the same track lands while the path awaits
					state.deck = picked.stable_id;
				}
				if (!stillNewest(0)) return state.deck === picked.stable_id;
				state.loads += 1;
				state.deck = picked.stable_id;
				if (state.rivalAfter) state.loads += 1; // another load sent right behind this one
				return state.deck === picked.stable_id;
			},
			() => 'old|1|0|paused',
			{ loadsDispatched: () => state.loads, commandsDispatched: () => 0, pending: () => 0, idle: async () => true }
		);
	const own = { loads: 3, deck: 'old' };
	assert.equal(await make(own)(row, 2), true, 'control: its own load, nothing else, is this pick\'s');
	const rival = { loads: 3, deck: 'old', rivalFirst: true };
	assert.equal(await make(rival)(row, 2), false, 'the path stood down; the same track on the deck is the rival\'s');
	const behind = { loads: 3, deck: 'old', rivalAfter: true };
	assert.equal(await make(behind)(row, 2), false, 'a load sent after this one owns the deck now');
});

test('CMDK-16: an older load that commits during the rb-meta wait does not drop the pick (Sol r4278366185)', async () => {
	const row = { stable_id: 'cmdk-pick', file_exists: null, file_availability: 'AVAILABILITY_PENDING' };
	const run = async (state) => {
		const loaded = [];
		const pick = load.createPickLoader(
			async () => {
				// The older load commits while rb-meta answers, before anyone waits on the queue.
				if (state.commitsDuringMeta) {
					state.mark = 'older-load|2|0|paused';
					state.pending = 0;
				}
				return { file_exists: true, is_streaming: false };
			},
			async (picked, deck) => {
				loaded.push([picked.stable_id, deck]);
				return true;
			},
			() => state.mark,
			{
				loadsDispatched: () => 3,
				commandsDispatched: () => 0,
				pending: () => state.pending,
				idle: async () => {
					state.waited += 1;
					return true;
				}
			}
		);
		return { ok: await pick(row, 2), loaded };
	};
	const older = { mark: 'old|1|0|paused', pending: 1, waited: 0, commitsDuringMeta: true };
	const got = await run(older);
	assert.equal(got.ok, true);
	assert.deepEqual(got.loaded, [['cmdk-pick', 2]], 'the pick replaces what the older load left');
	assert.equal(older.waited, 1, 'the older queue was drained alongside rb-meta');
	// Control: with nothing queued at the start, a mark that moves during the wait is someone else's.
	const moved = { mark: 'old|1|0|paused', pending: 0, waited: 0, commitsDuringMeta: true };
	const ctl = await run(moved);
	assert.equal(ctl.ok, false);
	assert.deepEqual(ctl.loaded, [], 'a deck that moved with nothing queued before the pick keeps its track');
	assert.equal(load.sameLoadAs('b|2|0|playing', 'a|1|0|paused'), 'b|2|0|paused');
	// The reservation a browser load took during the drain is not the pick's baseline (Sol P1, #6689).
	assert.equal(load.sameLoadAs('b|2|7|playing', 'a|1|3|paused'), 'b|2|3|paused', 'the pick keeps its own reservation');
});

test('CMDK-12: "gothic," with punctuation picks Gothic too; ranking, hint and Enter share the settings search\'s words (Sol r4278366200)', () => {
	assert.deepEqual(actions.actionTokens('gothic,'), ['gothic']);
	assert.deepEqual(actions.actionTokens('  UI skin: gothic! '), ['ui', 'skin', 'gothic']);
	const def = catalog.SETTINGS_CATALOG.find((d) => d.id === 'ui_skin');
	assert.equal(actions.settingNextValue(def, 'default', actions.actionTokens('gothic,')), 'mono-dev', 'Enter switches to Gothic');
	assert.equal(actions.settingVerb(def, 'default', actions.actionTokens('gothic,')), 'Enter for Gothic (mono dev preview)');
	assert.equal(actions.rankSettings(settingRows('gothic,', { ui_skin: 'default' }), 'gothic,')[0].id, 'ui_skin');
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	assert.match(bar, /const settingTokens = \$derived\(actionTokens\(query\)\);/);
	assert.match(bar, /\{settingVerb\(action\.def, action\.value, settingTokens\)\}/);
	assert.match(bar, /settingNextValue\(action\.def, action\.value, actionTokens\(query\)\)/);
});

test('CMDK-14: Space in the search input cancels the Eject confirm; the copy reads the deck as it is now (Sol r4278366197, r4278366191)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const input = bar.slice(bar.indexOf('} else if (ejectGuard !== null) {'), bar.indexOf('async function confirmEject'));
	assert.match(input, /if \(event\.key === 'Enter' && quickModifierHeld\(event, quickMod\)\) void confirmEject\(\);\s*(?:\/\/[^\n]*\s*)*else if \(event\.key === 'Enter' \|\| event\.key === 'Escape' \|\| event\.key === ' '\) ejectGuard = null;/);
	const box = bar.slice(bar.indexOf('{@const held = decks[ejectGuard.id]}'), bar.indexOf('</div></li>', bar.indexOf('{@const held = decks[ejectGuard.id]}')));
	// Playing, paused and empty each say what the deck holds now; only a loaded deck offers the eject.
	assert.match(box, /\{#if held\.stable_id === null\}\s*<span>Deck \{ejectGuard\.id\} is empty now; there is nothing to eject\.<\/span>/);
	assert.match(box, /\{:else if held\.playing\}\s*<span>Deck \{ejectGuard\.id\} is playing/);
	assert.match(box, /\{:else\}\s*<span>Deck \{ejectGuard\.id\} holds[^<]*, paused\. Eject it\?<\/span>/);
	assert.match(box, /\{#if held\.stable_id !== null\}\s*<button type="button" class="confirm"/);
	assert.match(box, /\{held\.playing \? 'Keep playing' : 'Cancel'\} · Esc/);
	assert.doesNotMatch(box, /aria-label="Eject a playing deck"/, 'the label is not fixed to playing');
});

test('CMDK-11: a typed query the settings search has no words for still searches tracks (Sol r4279823886)', () => {
	assert.deepEqual(actions.actionTokens('東京'), [], 'the precondition: the settings tokens are empty');
	const typed = layout('東京');
	assert.deepEqual(typed.groups.map((g) => g.id).slice(0, 2), ['playlist', 'collection']);
	assert.equal(typed.keepTyping, false);
	assert.ok(!typed.groups.some((g) => g.id === 'recent'), 'not the nothing-typed layout');
	// One non-ASCII character is the one-character list, as for ASCII.
	assert.deepEqual(layout('東').groups.map((g) => g.id), ['recent']);
	assert.equal(layout('東').keepTyping, true);
	// Controls: nothing typed, or only spaces, is still the open layout.
	for (const q of ['', '   ']) assert.deepEqual(layout(q).groups.map((g) => g.id), ['recent', 'commands', 'playlist'], JSON.stringify(q));
});

test('CMDK-12: an enum value that is not one of its options is named and refused, never stepped past (Sol r4279823899)', () => {
	const def = catalog.SETTINGS_CATALOG.find((d) => d.id === 'ui_skin');
	assert.equal(actions.enumValueKnown(def, 'default'), true);
	assert.equal(actions.enumValueKnown(def, 'retired-skin'), false);
	assert.throws(() => actions.enumTarget(def, 'retired-skin', []), /holds "retired-skin", which is not one of its options/);
	assert.throws(() => actions.settingNextValue(def, 'retired-skin', ['gothic']), /not one of its options/);
	assert.equal(actions.settingValueLabel(def, 'retired-skin'), 'retired-skin (not an option)');
	assert.equal(actions.settingVerb(def, 'retired-skin', []), 'Not one of its options; fix it in Settings');
	// Control: a known value still steps to the next option.
	assert.equal(actions.enumTarget(def, 'light', []), 'default');
	// Enter's refusal reaches the bar's error line, not an uncaught throw.
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const body = bar.slice(bar.indexOf('async function runAction'), bar.indexOf('const command = action.command;'));
	assert.ok(body.indexOf('try {') < body.indexOf('settingNextValue('), 'the next value is read inside the try');
});

test('an eject confirm does not run when a play was queued on the deck after the guard was taken (Codex P1 BLOCKING, #6689)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const confirm = bar.slice(bar.indexOf('async function confirmEject()'), bar.indexOf('const GROUP_NAME'));
	assert.match(confirm, /const commandsAt = ejectCommands;/);
	assert.match(confirm, /deckCommands\(guard\.id\) !== commandsAt/, 'a play since the guard makes the confirm ask again');
	assert.ok(confirm.indexOf('deckCommands(guard.id) !== commandsAt') < confirm.indexOf('runAction(action, true)'), 'the check runs before the eject');
	assert.match(bar, /ejectGuard = replaceGuard\(command\.deck[^\n]*\n\s*ejectCommands = deckCommands\(command\.deck\);/);
});

test('CMDK-14: an eject whose deck took a load during the initial drain is not unloaded (Codex P1, #6689)', () => {
	const bar = readFileSync(new URL('../../src/lib/components/rb/CommandBarV2.svelte', import.meta.url), 'utf8');
	const body = bar.slice(bar.indexOf('if (deckPending(command.deck) > 0) {'), bar.indexOf("const result = await runCommand({ type: 'unload', deck: command.deck }"));
	const loads = body.indexOf('const loads = deckLoadDispatches(command.deck);');
	const wait = body.indexOf('waitDeckIdle(');
	const check = body.indexOf('deckLoadDispatches(command.deck) !== loads');
	assert.ok(loads > 0 && loads < wait, 'the load count is taken before the drain');
	assert.ok(check > wait, 'the count is compared after the drain and before the eject');
});

test('CMDK-15: the bar opens at once so typing lands in the search; Enter waits for settings and a superseded open does nothing (Sol P1, #6689)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const open = bar.slice(bar.indexOf('async function openBar'), bar.indexOf('function closeBar'));
	// The bar is open before the settings module loads, so keys typed in that window reach the query.
	assert.ok(open.indexOf('open = true') > 0 && open.indexOf('open = true') < open.indexOf('loadSettingsApi()'), 'open is published before the settings load');
	// The settings load is started once and shared, so a second open during it awaits the same promise (Sol P1, #6689).
	assert.match(open, /openSettings = actionsOn \? loadSettingsApi\(\) : Promise\.resolve\(null\);/);
	assert.match(open, /const failure = await openSettings;/);
	// Enter is refused while the module is still loading, so a setting-only query cannot pick the next row.
	const act = bar.slice(bar.indexOf('function activate('), bar.indexOf('function activate(') + 400);
	assert.match(act, /if \(settingsLoading\) \{/);
	// A close or a newer open supersedes an in-flight open, so the older one never publishes its state.
	assert.match(open, /if \(life !== barLife\) return null;/);
	assert.match(bar.slice(bar.indexOf('function closeBar'), bar.indexOf('function closeBar') + 80), /barLife \+= 1;/);
	// A settings failure reaches the caller, and a bar closed during the load is an error, not a success.
	const adapter = bar.slice(bar.indexOf('let openTurn = 0;'), bar.indexOf('close: () => closeBar()'));
	assert.ok(adapter.indexOf('const turn = ++openTurn;') < adapter.indexOf('await openBar()'), 'the turn is taken before the open is awaited');
	assert.ok(adapter.indexOf('if (turn !== openTurn) return;') > adapter.indexOf('await openBar()'), 'a stale call stops before it sets the query');
	assert.match(adapter, /open \? await openSettings : await openBar\(\)/);
	// The failure is thrown before the stale check, so a superseded opener is told too.
	assert.ok(adapter.indexOf('if (failure !== null) throw new Error(failure);') < adapter.indexOf('if (turn !== openTurn) return;'), 'a settings failure reaches a superseded opener');
	assert.match(adapter, /if \(failure !== null\) throw new Error\(failure\);/);
	assert.match(adapter, /if \(!open\) throw new Error\('the Cmd K bar closed before it opened/);
});

test('Sol P1, #6689: closing or reopening the bar ends a pending eject, a drained deck that emptied is left alone, and the confirm chord is the platform modifier', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const close = bar.slice(bar.indexOf('function closeBar'), bar.indexOf('function closeBar') + 200);
	assert.match(close, /barLife \+= 1;[\s\S]*?ejectOp \+= 1;/, 'closing invalidates an eject still draining');
	const adapter = bar.slice(bar.indexOf('let openTurn = 0;'), bar.indexOf('close: () => closeBar()'));
	assert.match(adapter, /ejectGuard = null;\s*ejectOp \+= 1;/, 'a reopen with a query clears the eject guard and any pending eject');
	const eject = bar.slice(bar.indexOf('async function runAction'), bar.indexOf('async function runAction') + 6000);
	assert.match(eject, /if \(!decks\[command\.deck\]\.stable_id\) return null;/, 'an eject that drained onto an emptied deck is left alone');
	assert.match(bar, /quickModifierHeld\(event, quickMod\)\) void confirmEject/);
});

test('CMDK-14: an eject that waited out a queued command installs its confirmation only in the bar it was asked in (Codex r4280339710)', () => {
	const bar = readFileSync(`${SRC}lib/components/rb/CommandBarV2.svelte`, 'utf8');
	const body = bar.slice(bar.indexOf('async function runAction'), bar.indexOf('/** Enter, a click or a quick pick on any row. */'));
	assert.match(body, /if \(barSession === session\) \{\s*ejectGuard = replaceGuard\(command\.deck, decks\[command\.deck\], deckLoadDispatches\(command\.deck\), deckCommands\(command\.deck\)\);/);
	assert.match(body, /if \(barSession === session\) ejectGuard = null;/);
});
