// requirement: BACKSTAGE-6529, BACKSTAGE-6529b, BACKSTAGE-6529c, BACKSTAGE-6529d, BACKSTAGE-6529e
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let bs;
/** Every item in render order, as BackstageNav renders them. */
const allItems = (nav) => [
	nav.gig,
	...nav.backstage.flatMap((g) => g.items),
	...nav.developer.flatMap((g) => g.items)
];
let side;
let tab;
const LAYOUT = readFileSync(new URL('../../src/routes/+layout.svelte', import.meta.url), 'utf8');

before(async () => {
	bs = await loadTypeScriptModule('src/lib/shell/backstage-nav.ts');
	side = await loadTypeScriptModule('src/lib/shell/sidebar-nav.ts');
	tab = await loadTypeScriptModule('src/routes/admin/admin-tab.ts');
});

test('flag off: shell.backstage defaults off and the flat sidebar is unchanged', () => {
	const flag = readFileSync(
		new URL('../../src/lib/shell/backstage-flag.ts', import.meta.url),
		'utf8'
	);
	assert.match(
		flag,
		/flag\.flag_id === 'shell\.backstage'\)\?\.enabled === true/,
		'undeclared or unread flag is off'
	);
	assert.deepEqual(
		side.sidebarNavLinks(false).map((l) => l.label),
		[
			'Library',
			'Pairings',
			'Smartlists',
			'Missing tracks',
			'Dedup Review',
			'Performance',
			'Play analytics',
			'Library wheel',
			'Sessions / REC',
			'CloudSync',
			'Plugins',
			'Settings'
		]
	);
	// The old list still renders in the flag-off branch, untouched.
	assert.match(
		LAYOUT,
		/\{#if backstageOn && BackstageNav\}\s*<BackstageNav[^]*?\{:else\}\s*<h1>Open DJ<\/h1>\s*<nav>/
	);
	assert.match(LAYOUT, /\{#each navLinks as link \(link\.href\)\}/);
});

test('flag on: Gig first, then the Backstage groups in order', () => {
	const nav = bs.backstageNav(false);
	assert.equal(nav.gig.label, 'Gig');
	assert.equal(nav.gig.href, '/performance');
	assert.equal(nav.gig.title, 'The core Open DJ rig');
	assert.deepEqual(
		nav.backstage.map((g) => g.label),
		['Overview', 'Library', 'Generate', 'Sets', 'Lyrics', 'AI', 'Sync', 'Settings']
	);
	const labels = Object.fromEntries(nav.backstage.map((g) => [g.id, g.items.map((i) => i.label)]));
	assert.deepEqual(labels.library, [
		'Library',
		'Pairings',
		'Smartlists',
		'Library wheel',
		'Missing tracks',
		'Dedup Review'
	]);
	assert.deepEqual(labels.gen, ['Stems', 'Genres', 'Analysis', 'Vocals']);
	assert.deepEqual(labels.sets, ['Sessions / REC', 'Play analytics']);
	assert.deepEqual(labels.sync, ['CloudSync']);
	assert.deepEqual(labels.prefs, ['Settings', 'Setup', 'Plugins']);
	assert.deepEqual(labels.overview, ['Farm jobs', 'Status']);
	assert.equal(nav.backstage[0].items[0].href, '/admin?p=farm-jobs');
	assert.equal(nav.backstage[0].items[1].href, '/admin?p=status');
	assert.equal(nav.backstage[0].items[1].inert, undefined);
	assert.equal(allItems(nav)[0].id, 'gig');
});

test('Generate items without a page are inert with the PARITY-TODO tooltip', () => {
	const gen = bs.backstageNav(false).backstage.find((g) => g.id === 'gen').items;
	for (const it of gen.filter((i) => i.id !== 'analysis')) {
		assert.equal(it.href, null);
		assert.equal(it.inert, 'unbuilt');
		assert.equal(it.title, 'not implemented - see PARITY-TODO');
	}
	assert.equal(gen.find((i) => i.id === 'analysis').href, '/analysis-backfill');
});

test('AI items are inert, titled "not built yet", and skipped by keyboard', () => {
	const nav = bs.backstageNav(true);
	const ai = nav.backstage.find((g) => g.id === 'ai').items;
	assert.deepEqual(
		ai.map((i) => i.label),
		['AI Studio', 'AI Workbench', 'Benchmarking']
	);
	for (const it of ai) {
		assert.equal(it.inert, 'soon');
		assert.equal(it.href, null);
		assert.equal(it.title, `${it.label} - not built yet, see PARITY-TODO`);
	}
	// Inert items have no href, so BackstageNav renders them as spans that W/A/S/D never visit.
	const focusable = allItems(nav)
		.filter((i) => i.href !== null)
		.map((i) => i.id);
	for (const id of ['aistudio', 'aiwb', 'bench', 'stems', 'genres', 'vocals']) {
		assert.ok(!focusable.includes(id), `${id} must not be focusable`);
	}
});

test('Developer groups are hidden without show_dev_ui and shown with it', () => {
	const off = bs.backstageNav(false);
	assert.deepEqual(off.developer, []);
	const offIds = allItems(off).map((i) => i.id);
	for (const id of [
		'perf',
		'farm',
		'quality',
		'health',
		'bus',
		'flags',
		'preflight',
		'ports',
		'api',
		'sql',
		'queues',
		'progress'
	]) {
		assert.ok(!offIds.includes(id), `${id} leaked without show_dev_ui`);
	}
	const on = bs.backstageNav(true);
	assert.deepEqual(
		on.developer.map((g) => g.label),
		['Metrics', 'System', 'Tools']
	);
	assert.deepEqual(
		on.developer.flatMap((g) => g.items.map((i) => i.id)),
		[
			'perf',
			'farm',
			'quality',
			'health',
			'bus',
			'flags',
			'preflight',
			'ports',
			'api',
			'sql',
			'queues',
			'progress'
		]
	);
});

test('W/A/S/D map to the arrow keys; modifiers jump to the ends; never wraps', () => {
	for (const [k, a] of [
		['w', 'ArrowUp'],
		['W', 'ArrowUp'],
		['a', 'ArrowLeft'],
		['s', 'ArrowDown'],
		['D', 'ArrowRight'],
		['ArrowDown', 'ArrowDown']
	]) {
		assert.equal(bs.navArrowKey(k), a);
	}
	assert.equal(bs.navArrowKey('x'), null);
	assert.equal(bs.nextNavIndex(2, 5, 'ArrowDown', false), 3);
	assert.equal(bs.nextNavIndex(2, 5, bs.navArrowKey('s'), false), 3);
	assert.equal(bs.nextNavIndex(2, 5, bs.navArrowKey('w'), false), 1);
	assert.equal(bs.nextNavIndex(4, 5, 'ArrowRight', false), 4);
	assert.equal(bs.nextNavIndex(0, 5, 'ArrowUp', false), 0);
	assert.equal(bs.nextNavIndex(2, 5, bs.navArrowKey('S'), true), 4);
	assert.equal(bs.nextNavIndex(2, 5, bs.navArrowKey('A'), true), 0);
	assert.equal(bs.nextNavIndex(-1, 5, 'ArrowDown', false), 0);
	// The component routes every nav keydown through moveFocusAmong (exercised below).
	const comp = readFileSync(
		new URL('../../src/lib/shell/BackstageNav.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		comp,
		/moveFocusAmong\(\s*event,\s*Array\.from\(root\.querySelectorAll<HTMLAnchorElement>\('a\[data-nav-item\]'\)\),\s*document\.activeElement\s*\)/
	);
});

/** Fake focusable links for the real non-inert nav items, plus a key event recorder. */
function navHarness() {
	const ids = allItems(bs.backstageNav(true))
		.filter((i) => i.href !== null)
		.map((i) => i.id);
	const state = { focused: null };
	const links = ids.map((id) => ({
		id,
		focus() {
			state.focused = this;
		}
	}));
	/** Press `key` on `target` with `mods`; returns whether focus moved and whether default was prevented. */
	const press = (key, active, mods = {}, target = active) => {
		let prevented = false;
		const event = {
			key,
			target,
			shiftKey: false,
			ctrlKey: false,
			altKey: false,
			metaKey: false,
			...mods,
			preventDefault() {
				prevented = true;
			}
		};
		state.focused = active;
		const moved = bs.moveFocusAmong(event, links, active);
		return { moved, prevented, to: state.focused?.id ?? null };
	};
	return { links, press };
}

test('W/A on a backstage link step back one and stop at the first link', () => {
	const { links, press } = navHarness();
	for (const key of ['w', 'W', 'a', 'A']) {
		assert.deepEqual(press(key, links[3]), { moved: true, prevented: true, to: links[2].id }, key);
		assert.deepEqual(press(key, links[0]), { moved: true, prevented: true, to: links[0].id }, key);
	}
	assert.equal(links[0].id, 'gig');
});

test('S/D on a backstage link step forward one and stop at the last link', () => {
	const { links, press } = navHarness();
	const last = links.length - 1;
	for (const key of ['s', 'S', 'd', 'D']) {
		assert.deepEqual(press(key, links[3]), { moved: true, prevented: true, to: links[4].id }, key);
		assert.deepEqual(
			press(key, links[last]),
			{ moved: true, prevented: true, to: links[last].id },
			key
		);
	}
	assert.equal(links[last].id, 'progress');
	// Inert items are never visited: stepping from Library jumps the unbuilt Generate items.
	const fromLib = links.findIndex((l) => l.id === 'dedup');
	assert.equal(press('s', links[fromLib]).to, 'analysis');
});

test('a held modifier jumps W/A to the first link and S/D to the last', () => {
	const { links, press } = navHarness();
	const last = links[links.length - 1].id;
	for (const mod of ['shiftKey', 'ctrlKey', 'altKey', 'metaKey']) {
		for (const key of ['w', 'a']) assert.equal(press(key, links[5], { [mod]: true }).to, 'gig');
		for (const key of ['s', 'd']) assert.equal(press(key, links[5], { [mod]: true }).to, last);
	}
});

test('a focused text field keeps W/A/S/D; other keys are ignored', () => {
	const { links, press } = navHarness();
	const fields = [
		{ tagName: 'INPUT', type: 'text' },
		{ tagName: 'INPUT', type: 'search' },
		{ tagName: 'TEXTAREA' },
		{ tagName: 'DIV', isContentEditable: true },
		{ tagName: 'DIV', getAttribute: (n) => (n === 'role' ? 'textbox' : null) }
	];
	for (const field of fields) {
		for (const key of ['w', 'a', 's', 'd', 'ArrowDown']) {
			const r = press(key, links[3], {}, field);
			assert.equal(r.moved, false, `${field.tagName} ${key}`);
			assert.equal(r.prevented, false, `${field.tagName} ${key}`);
			assert.equal(r.to, links[3].id);
		}
	}
	assert.deepEqual(press('x', links[3]), { moved: false, prevented: false, to: links[3].id });
	// Nothing focused yet: S lands on the first link, W on the last.
	assert.equal(press('s', null).to, 'gig');
	assert.equal(press('w', null).to, 'progress');
});

test('/admin?p=<id> picks one panel; unknown ids are refused; absent is the old page', () => {
	const u = (q) => new URL(`http://x/admin${q}`);
	assert.deepEqual(tab.adminPanelFromUrl(u('')), { kind: 'none' });
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=farm-jobs')), { kind: 'panel', id: 'farm-jobs' });
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=status')), { kind: 'panel', id: 'status' });
	assert.deepEqual(tab.adminPanelFromUrl(u('?p=nope')), { kind: 'unknown', id: 'nope' });
	const ids = allItems(bs.backstageNav(true))
		.filter((i) => i.href?.startsWith('/admin?p='))
		.map((i) => new URL(i.href, 'http://x').searchParams.get('p'));
	for (const id of ids) assert.equal(tab.adminPanelFromUrl(u(`?p=${id}`)).kind, 'panel', id);
	assert.equal(bs.isCurrentBackstageItem({ href: '/admin?p=jobs' }, u('?p=jobs')), true);
	assert.equal(bs.isCurrentBackstageItem({ href: '/admin?p=jobs' }, u('?p=sources')), false);
	assert.equal(bs.isCurrentBackstageItem({ href: '/admin?p=farm-jobs' }, u('?p=farm-jobs')), true);
});

test('flag off leaves the account overlay unchanged: shell.backstage is not listed as a missing capability', () => {
	const overlay = readFileSync(
		new URL('../../src/lib/components/account/AccountOverlay.svelte', import.meta.url),
		'utf8'
	);
	assert.match(
		overlay,
		/storeBuildRefusal\(flag\.flag_id\) === null &&[^]*?flag\.flag_id !== 'shell\.backstage'/
	);
});

test("the lazy-bundle copies of the flag id are all 'shell.backstage'", () => {
	for (const rel of [
		'../../src/lib/shell/backstage-flag.ts',
		'../../src/lib/components/account/AccountOverlay.svelte'
	]) {
		const src = readFileSync(new URL(rel, import.meta.url), 'utf8');
		assert.ok(src.includes("'shell.backstage'"), rel);
	}
});
