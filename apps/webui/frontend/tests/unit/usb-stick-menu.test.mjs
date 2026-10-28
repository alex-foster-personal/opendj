// requirement: USBEXP-04
/**
 * A USB stick's right-click rows (USB status, Sync to stick, Eject stick) in
 * Library mode, not only in Gig mode.
 *
 * Library mode is routes/+page.svelte -> CrateDesk -> BrowserPanel, which
 * mounts no Quick Draw menu. The rows used to exist only in Quick Draw, so a
 * right-click on a stick there showed nothing. They now come from the stick
 * tree itself (UsbStickTree -> usbStickMenuItems -> the registry), which both
 * workspaces mount.
 *
 * What runs here: the real registry and the real binding, rendered through the
 * real ContextMenu component with svelte/server. No fetch is made and none is
 * stubbed: no row is clicked. What a pass does NOT cover: a browser
 * right-click on a mounted stick, which needs a real stick (see
 * tests/e2e/usb-export-screen.spec.ts, which reports UNAVAILABLE without one).
 *
 * Regression one-liners:
 *   - if Library mode's stick tree opens no menu of its own then broken
 *   - if the stick rows render only through Quick Draw then broken
 *   - if Quick Draw's window listener can replace the stick menu then broken
 *   - if a stick-less read-only tree gains the device rows then broken
 *   - if Eject is offered with no mount path to eject then broken
 *   - if Eject is offered before a current healthy scan lists this stick at this path then broken
 *   - if Eject starts before the screen that holds the page is mounted then broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

function source(relativePath) {
	return readFileSync(fileURLToPath(new URL(`../../${relativePath}`, import.meta.url)), 'utf8');
}

const ENTRY = [
	"export { default as ContextMenu } from '$lib/components/rb/ContextMenu.svelte';",
	"export { usbStickMenuItems, ejectOnceScreenIsUp, EJECT_UNVERIFIED, EJECT_NOT_STARTED_LOAD, EJECT_NOT_STARTED_UNMOUNTED } from '$lib/rb/usb-stick-menu';",
	"export { countUsbExportScreenMount, usbExportScreenMounted } from '$lib/rb/usb-export.svelte';",
	"export { menuFor } from '$lib/context-menu/registry';",
	"export { render } from 'svelte/server';"
].join('\n');

let mod;

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
});

const STICK = { id: 'vol:A1B2C3D4-0000-0000-0000-00000000000A', mount_path: '/Volumes/GIG-A' };
const LIVE_A = { ...STICK, name: 'GIG-A', present: true, role: 'usb_stick', protocol: 'USB' };
const HEALTHY = { volumes: [LIVE_A], scannedAt: 1, discovery: 'ok' };

function rendered(items) {
	return mod.render(mod.ContextMenu, { props: { items, x: 10, y: 20, onclose: () => {} } }).body;
}

test('a stick tree binds status and eject to runners, and sync stays disabled with its reason', () => {
	const items = mod.usbStickMenuItems(mod.menuFor, STICK, HEALTHY);
	assert.deepEqual(
		items.map((i) => [i.id, i.label, typeof i.run]),
		[
			['usb-status', 'USB status...', 'function'],
			['usb-sync', 'Sync to stick', 'undefined'],
			['usb-eject', 'Eject stick', 'function']
		]
	);
	assert.match(items[1].title, /Export to USB/, 'the disabled row says why');
});

test('Eject waits for a current healthy scan that lists this stick at this path', () => {
	const eject = (tracker, stick = STICK) => mod.usbStickMenuItems(mod.menuFor, stick, tracker).find((i) => i.id === 'usb-eject');
	const unverified = [
		['no scan yet this session', { ...HEALTHY, scannedAt: null }],
		['discovery refused', { ...HEALTHY, discovery: 'unavailable' }],
		['daemon unreachable', { ...HEALTHY, discovery: 'unreachable' }],
		['remembered, not present', { ...HEALTHY, volumes: [{ ...LIVE_A, present: false }] }],
		['another stick now at this path', { ...HEALTHY, volumes: [{ ...LIVE_A, id: 'vol:A1B2C3D4-0000-0000-0000-00000000000B' }] }],
		['this stick moved to another path', { ...HEALTHY, volumes: [{ ...LIVE_A, mount_path: '/Volumes/GIG-A 1' }] }]
	];
	for (const [why, tracker] of unverified) {
		const row = eject(tracker);
		assert.equal(row.run, undefined, why);
		assert.equal(row.title, mod.EJECT_UNVERIFIED, why);
		// USB status still opens the screen, which explains the scan state.
		const status = mod.usbStickMenuItems(mod.menuFor, STICK, tracker).find((i) => i.id === 'usb-status');
		assert.equal(typeof status.run, 'function', why);
	}
	// Control: the same stick on a healthy scan is ejectable.
	assert.equal(typeof eject(HEALTHY).run, 'function');
	assert.notEqual(eject(HEALTHY).title, mod.EJECT_UNVERIFIED);
});

test('Eject is not offered for a listed stick with no volume UUID, and says why (Sol P1 4398681354)', () => {
	// The scanner's path: fallback id: the daemon refuses every such eject with
	// eject_identity_unverifiable, so offering it is a control that never works.
	const noUuid = { ...STICK, id: 'path:GIG-A' };
	const tracker = { ...HEALTHY, volumes: [{ ...LIVE_A, id: noUuid.id }] };
	const row = mod.usbStickMenuItems(mod.menuFor, noUuid, tracker).find((i) => i.id === 'usb-eject');
	assert.equal(row.run, undefined);
	assert.match(row.title, /no volume UUID/);
	for (const id of ['vol:', 'stick-a']) {
		const other = mod.usbStickMenuItems(mod.menuFor, { ...STICK, id }, { ...HEALTHY, volumes: [{ ...LIVE_A, id }] }).find((i) => i.id === 'usb-eject');
		assert.equal(other.run, undefined, id);
	}
	// Control: the same stick listed with its UUID is ejectable.
	assert.equal(typeof mod.usbStickMenuItems(mod.menuFor, STICK, HEALTHY).find((i) => i.id === 'usb-eject').run, 'function');
});

test('the three rows render in the real ContextMenu', () => {
	const html = rendered(mod.usbStickMenuItems(mod.menuFor, STICK, HEALTHY));
	for (const label of ['USB status...', 'Sync to stick', 'Eject stick']) {
		assert.ok(html.includes(label), `${label} rendered`);
	}
	// Control: the same render without a stick has none of them.
	const none = rendered(mod.usbStickMenuItems(mod.menuFor, { id: STICK.id, mount_path: null }, HEALTHY));
	assert.ok(!none.includes('Eject stick'));
	assert.deepEqual(mod.usbStickMenuItems(mod.menuFor, { id: STICK.id, mount_path: null }, HEALTHY), []);
});

test('Library mode reaches the stick tree, and mounts no Quick Draw', () => {
	const page = source('src/routes/+page.svelte');
	assert.match(page, /import\('\$lib\/components\/library\/CrateDesk\.svelte'\)/);
	const desk = source('src/lib/components/library/CrateDesk.svelte');
	assert.match(desk, /<BrowserPanel /);
	// The reason the rows have to live on the tree: Library mode has no Quick Draw.
	assert.doesNotMatch(page + desk, /QuickDraw/);
	assert.match(source('src/routes/performance/+page.svelte'), /QuickDrawMenuLoader/, 'control: the probe sees a Quick Draw mount');
	const list = source('src/lib/components/rb/browser/UsbSourceList.svelte');
	assert.match(list, /<UsbStickTree /);
});

test('the stick tree opens its own menu and stops the event before Quick Draw sees it', () => {
	const tree = source('src/lib/components/rb/browser/UsbStickTree.svelte');
	assert.match(tree, /role="tree"[^>]*oncontextmenu=\{_openMenu\}/);
	const open = tree.slice(tree.indexOf('function _openMenu('), tree.indexOf('function _openMenuFromKeyboard('));
	assert.match(open, /event\.preventDefault\(\);/);
	assert.match(open, /event\.stopPropagation\(\);/);
	assert.match(open, /loadMenuRegistry\(reportMenuLoadFailure\)/);
	assert.match(tree, /items=\{usbStickMenuItems\(menuRegistry\(\)!\.menuFor, volume, usbTracker\)\}/);
	// The registry stays lazy: a static import would put every menu on first paint.
	assert.doesNotMatch(tree, /from '\$lib\/context-menu\/registry'/);
	// One path: Quick Draw no longer carries its own copy of the stick rows.
	const quickDraw = source('src/lib/components/rb/QuickDrawMenu.svelte');
	assert.doesNotMatch(quickDraw, /usb-eject|ejectUsbStick|stick: true/);
	assert.match(quickDraw, /\{ stick: false \}/);
});

test('every listed stick row opens the stick menu, collapsed or unreadable (Codex P2 4302000930)', () => {
	const list = source('src/lib/components/rb/browser/UsbSourceList.svelte');
	// The handlers sit on the row itself, outside the access and expanded
	// branches that decide whether the tree mounts.
	const row = list.slice(list.indexOf('data-testid="usb-source-row"'), list.indexOf('<svg', list.indexOf('data-testid="usb-source-row"')));
	assert.match(row, /oncontextmenu=\{\(e\) => _openRowMenu\(e, vol\)\}/);
	assert.match(row, /onkeydown=\{\(e\) => _rowKeydown\(e, vol\)\}/);
	assert.ok(list.indexOf('data-testid="usb-source-row"') < list.indexOf("{#if vol.access === 'pending'}"), 'the row is drawn before the access branches');
	const open = list.slice(list.indexOf('function _openRowMenu('), list.indexOf('function _rowKeydown('));
	assert.match(open, /event\.preventDefault\(\);/);
	assert.match(open, /event\.stopPropagation\(\);/);
	assert.match(open, /loadMenuRegistry\(reportMenuLoadFailure\)/);
	const keys = list.slice(list.indexOf('function _rowKeydown('), list.indexOf('</script>', list.indexOf('function _rowKeydown(')));
	assert.match(keys, /event\.key === 'ContextMenu' \|\| \(event\.shiftKey && event\.key === 'F10'\)/);
	assert.match(keys, /else if \(event\.key === 'Enter'\) \{\s*_toggleStick\(vol\);/, 'control: Enter still opens the stick');
	assert.match(list, /items=\{usbStickMenuItems\(menuRegistry\(\)!\.menuFor, menu\.vol, usbTracker\)\}/);
	assert.doesNotMatch(list, /from '\$lib\/context-menu\/registry'/, 'the registry stays lazy');
});

// Records the order the eject steps ran in. No fetch and no stick: the steps
// are plain functions, and the real wiring is pinned in the next test.
function steps({ loadFails = false, mountedAfterSettles = 1 } = {}) {
	const log = [];
	let settles = 0;
	return {
		log,
		steps: {
			load: async () => {
				log.push('load');
				if (loadFails) throw new Error('chunk failed');
			},
			settle: async () => {
				settles += 1;
				log.push('settle');
			},
			screenMounted: () => mountedAfterSettles !== null && settles >= mountedAfterSettles,
			eject: async () => {
				log.push('eject');
			},
			notify: (message) => {
				log.push(`notify: ${message}`);
			}
		}
	};
}

test('Eject starts only once the screen is loaded and mounted (Codex P1 4287034869, 4291127157)', async () => {
	const ok = steps();
	assert.equal(await mod.ejectOnceScreenIsUp(ok.steps), true);
	assert.deepEqual(ok.log, ['load', 'settle', 'eject'], 'control: a mounted screen ejects, after its render');

	const failedLoad = steps({ loadFails: true });
	assert.equal(await mod.ejectOnceScreenIsUp(failedLoad.steps), false);
	assert.deepEqual(
		failedLoad.log,
		['load', `notify: ${mod.EJECT_NOT_STARTED_LOAD} (chunk failed). The stick is still mounted.`],
		'a failed load ejects nothing and says so (Sol P1 4398029451)'
	);

	// A navigation unmounted the loader while the chunk loaded: no screen to
	// hold the page or say when the stick is safe to pull.
	const gone = steps({ mountedAfterSettles: null });
	assert.equal(await mod.ejectOnceScreenIsUp(gone.steps), false);
	assert.ok(!gone.log.includes('eject'), 'if Eject starts with no screen mounted then broken');
	assert.deepEqual(
		gone.log,
		['load', 'settle', 'settle', 'settle', `notify: ${mod.EJECT_NOT_STARTED_UNMOUNTED}`],
		'the wait is bounded, and the user is told the stick is still mounted (Sol P1 4398029451)'
	);

	const late = steps({ mountedAfterSettles: 2 });
	assert.equal(await mod.ejectOnceScreenIsUp(late.steps), true, 'a screen that needs a second render still ejects');
	assert.deepEqual(late.log, ['load', 'settle', 'settle', 'eject']);

	const already = steps({ mountedAfterSettles: 0 });
	assert.equal(await mod.ejectOnceScreenIsUp(already.steps), true);
	assert.deepEqual(already.log, ['load', 'eject'], 'an open screen is not waited on');
});

test('the screen counts its own mount, and the menu Eject runs through that gate', () => {
	const a = mod.countUsbExportScreenMount();
	const b = mod.countUsbExportScreenMount();
	assert.equal(mod.usbExportScreenMounted(), true);
	a();
	a(); // a second cleanup of the same mount must not uncount the other one
	assert.equal(mod.usbExportScreenMounted(), true, 'one screen is still mounted');
	b();
	assert.equal(mod.usbExportScreenMounted(), false);

	// Source pins for the wiring: running the row would start a real eject.
	const screen = source('src/lib/components/rb/UsbExportScreen.svelte');
	assert.match(screen, /onMount\(countUsbExportScreenMount\);/);
	const menu = source('src/lib/rb/usb-stick-menu.ts');
	assert.match(
		menu,
		/openUsbExport\(\);\s*await ejectOnceScreenIsUp\(\{\s*load: loadUsbExportScreen,\s*settle: tick,\s*screenMounted: usbExportScreenMounted,\s*eject: \(\) => ejectUsbStick\(ejectable, volume\.id\),\s*notify: announceUsbProblem\s*\}\);/
	);
	// The notice is an error toast, raised by the tracker that owns the USB toasts.
	assert.match(
		source('src/lib/rb/usb-tracker.svelte.ts'),
		/export function announceUsbProblem\(message: string\): void \{\s*pushToast\(message, 'error'\);\s*\}/
	);
	// One fetch, shared with the mount point, so the menu waits on the same load.
	const loader = source('src/lib/components/rb/UsbExportScreenLoader.svelte');
	assert.match(loader, /loadUsbExportScreen\(\)\s*\.then\(\(component\) => \{\s*Screen = component;/);
	assert.doesNotMatch(loader, /import\('\$lib\/components\/rb\/UsbExportScreen\.svelte'\)/);
});
