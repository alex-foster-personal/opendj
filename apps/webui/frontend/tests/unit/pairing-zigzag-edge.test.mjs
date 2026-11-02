/**
 * PAIR-09 / PAIR-10 (the maintainer, Thu 8 Oct 2026): every paired row gets a purple
 * zigzag ring whose pulses travel around the row while a partner is on a deck, an off-screen
 * master partner gets a PAIR fold arrow, and right-click on exactly two rows
 * offers "Pair these 2 tracks", which makes a loose pair.
 *
 * Drives the real modules: the pure row/fold helpers TrackTable binds and the
 * menu registry. PairingGraph and the menu runner talk to the daemon, so
 * their load, failure, recovery and create paths run against the real engine
 * in tests/e2e/pairing-library-edge.spec.ts; here only their wiring is read. The Svelte AST checks prove TrackTable and BrowserPanel
 * bind those helpers, since SSR renders no virtualized rows (see
 * pairing-library-indicators.test.mjs for why).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { parse } from 'svelte/compiler';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const TRACK_TABLE = `${SRC}/lib/components/rb/browser/TrackTable.svelte`;
const PAIRING_INDEX = `${SRC}/lib/rb/pairing-index.svelte.ts`;
const PAIRING_GRAPH = `${SRC}/lib/rb/pairing-graph.svelte.ts`;
const BROWSER_PANEL = `${SRC}/lib/components/rb/BrowserPanel.svelte`;
const THEME = `${SRC}/lib/rb/theme.css`;

let rowMod;
let registry;

function pairing(id, from, to, snapshot = null) {
	return {
		pairing_id: id,
		from_stable_id: from,
		to_stable_id: to,
		direction: '<->',
		source: 'manual',
		notes: null,
		snapshot,
		created_at: '2026-10-08T00:00:00+00:00',
		updated_at: '2026-10-08T00:00:00+00:00'
	};
}

function* walk(node) {
	if (node === null || typeof node !== 'object') return;
	if (typeof node.type === 'string') yield node;
	for (const [key, value] of Object.entries(node)) {
		if (key === 'parent') continue;
		if (Array.isArray(value)) for (const child of value) yield* walk(child);
		else if (value && typeof value === 'object') yield* walk(value);
	}
}

const TRACK_CTX = {
	selection: 'many',
	source: 'collection',
	audio: 'present',
	stems: 'none',
	workspace: 'performance'
};

before(async () => {
	rowMod = await loadTypeScriptModule('src/lib/rb/pairing-row.ts');
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
});

describe('pairing row state', () => {
	it('links both directions and skips a self-pair', () => {
		const map = rowMod.buildPartnerMap([
			pairing('p1', 'a', 'b'),
			pairing('p2', 'c', 'a'),
			pairing('p3', 'd', 'd')
		]);
		assert.deepEqual([...map.get('a')].sort(), ['b', 'c']);
		assert.deepEqual([...map.get('b')], ['a']);
		assert.deepEqual([...map.get('c')], ['a']);
		assert.equal(map.has('d'), false, 'a self-pair is not a pairing');
	});

	it('is none for an unpaired row, still when no partner is loaded, live when one is', () => {
		const map = rowMod.buildPartnerMap([pairing('p1', 'a', 'b'), pairing('p2', 'a', 'c')]);
		assert.equal(rowMod.pairRowState(map, new Set(), 'z'), 'none');
		assert.equal(rowMod.pairRowState(map, new Set(), 'a'), 'still');
		// The control for "live": a loaded track that is NOT a partner keeps it still.
		assert.equal(rowMod.pairRowState(map, new Set(['z']), 'a'), 'still');
		assert.equal(rowMod.pairRowState(map, new Set(['c']), 'a'), 'live');
		// The loaded track itself is not live because of its own load.
		assert.equal(rowMod.pairRowState(map, new Set(['b']), 'b'), 'still');
	});

	it('the PAIR arrow targets the first partner row in table order', () => {
		const rows = [{ stable_id: 'x' }, { stable_id: 'p2' }, { stable_id: 'p1' }];
		assert.deepEqual(rowMod.partnerIndices(rows, new Set(['p1', 'p2'])), [1, 2]);
		assert.deepEqual(rowMod.partnerIndices(rows, new Set(['nope'])), []);
		assert.deepEqual(rowMod.partnerIndices(rows, new Set()), []);
	});

	it('the PAIR pointer weighs every partner: hidden if any is on screen, else the nearest', () => {
		// fold([a, b]) makes rows a..b the visible window.
		const fold = (vis) => (i) => (i < vis[0] ? 'above' : i > vis[1] ? 'below' : null);
		// A visible partner hides the pointer even when another is off screen.
		assert.equal(rowMod.nearestOffscreenPartner([2, 30], fold([0, 9]), 5), null);
		// Table order does not decide: a visible partner listed second still hides it.
		assert.equal(rowMod.nearestOffscreenPartner([30, 2], fold([0, 9]), 5), null);
		// All off screen on opposite edges: the nearer one wins, either direction.
		assert.deepEqual(rowMod.nearestOffscreenPartner([3, 60], fold([20, 29]), 25), { index: 3, fold: 'above' });
		assert.deepEqual(rowMod.nearestOffscreenPartner([3, 40], fold([20, 29]), 25), { index: 40, fold: 'below' });
		// Nearest among several on one side, not the first in table order.
		assert.deepEqual(rowMod.nearestOffscreenPartner([1, 15], fold([20, 29]), 25), { index: 15, fold: 'above' });
		assert.equal(rowMod.nearestOffscreenPartner([], fold([0, 9]), 5), null);
	});

	it('the PAIR arrow sits beside MASTER only when both point off the same edge', () => {
		const alone = rowMod.pairFoldCenterPx({
			titleCenterPx: 200,
			masterOnSameEdge: false,
			masterWidthPx: 80,
			pairWidthPx: 18
		});
		assert.equal(alone, 200);
		const beside = rowMod.pairFoldCenterPx({
			titleCenterPx: 200,
			masterOnSameEdge: true,
			masterWidthPx: 80,
			pairWidthPx: 18
		});
		// MASTER's right edge (240) + the gap + half the arrow: no overlap.
		assert.equal(beside, 240 + rowMod.PAIR_FOLD_GAP_PX + 9);
		assert.ok(beside - 9 > 200 + 40, 'arrow left edge clears the MASTER badge');
	});

	it('beside MASTER on a title scrolled off the left, the arrow still clears the badge', async () => {
		const anchor = await loadTypeScriptModule('src/lib/rb/master-fold-anchor.ts');
		const widths = { title: 200 };
		const scrollLeft = 5000; // the title column is far off to the left
		const masterW = 80;
		const pairW = 18;
		const masterCenter = anchor.masterFoldCenterPx(widths, scrollLeft, 1200, masterW / 2);
		const center = (masterOnSameEdge) =>
			rowMod.pairFoldCenterPx({
				titleCenterPx: anchor.masterFoldCenterPx(
					widths,
					scrollLeft,
					1200,
					rowMod.pairFoldClampHalfWidthPx({ masterOnSameEdge, masterWidthPx: masterW, pairWidthPx: pairW })
				),
				masterOnSameEdge,
				masterWidthPx: masterW,
				pairWidthPx: pairW
			});
		const beside = center(true);
		assert.equal(beside - pairW / 2, masterCenter + masterW / 2 + rowMod.PAIR_FOLD_GAP_PX);
		// Alone, the arrow clamps by its own half-width, flush with the wrap's edge.
		assert.equal(center(false), anchor.MASTER_FOLD_MIN_LEFT_PX + pairW / 2);
	});

	it('beside MASTER on a title scrolled off the right, MASTER and the arrow shift left together', async () => {
		const anchor = await loadTypeScriptModule('src/lib/rb/master-fold-anchor.ts');
		const wrap = 1200;
		const widths = { title: 200 };
		const scrollLeft = -5000; // the title column is far off to the right
		const masterW = 80;
		const pairW = 18;
		const masterCenter = anchor.masterFoldCenterPx(widths, scrollLeft, wrap, masterW / 2);
		assert.equal(masterCenter, wrap - anchor.MASTER_FOLD_MIN_LEFT_PX - masterW / 2, 'MASTER is right-clamped');
		// PAIR-09: the arrow stays 6px to MASTER's right, so the pair moves as one group.
		const group = rowMod.pairFoldGroupMasterCenterPx({
			masterCenterPx: masterCenter,
			masterWidthPx: masterW,
			pairWidthPx: pairW,
			wrapWidthPx: wrap
		});
		assert.ok(group < masterCenter, 'MASTER shifts left to make room for the arrow');
		const beside = rowMod.pairFoldCenterPx({
			titleCenterPx: group,
			masterOnSameEdge: true,
			masterWidthPx: masterW,
			pairWidthPx: pairW
		});
		assert.equal(beside - pairW / 2, group + masterW / 2 + rowMod.PAIR_FOLD_GAP_PX, 'arrow sits 6px right of MASTER');
		assert.ok(beside + pairW / 2 <= wrap - anchor.MASTER_FOLD_MIN_LEFT_PX, 'arrow right edge stays inside the wrap');
		assert.ok(group - masterW / 2 >= anchor.MASTER_FOLD_MIN_LEFT_PX, 'MASTER left edge stays inside the wrap');
		// Control: with room on the right, neither moves.
		assert.equal(
			rowMod.pairFoldGroupMasterCenterPx({ masterCenterPx: 200, masterWidthPx: masterW, pairWidthPx: pairW, wrapWidthPx: wrap }),
			200
		);
	});

	it('a failed pairing load stays visible on the table until a load succeeds', () => {
		const table = readFileSync(TRACK_TABLE, 'utf8');
		assert.match(table, /\n\t\tpairError: string \| null;/, 'pairError is a required prop');
		assert.match(
			table,
			/\{#if pairError !== null\}[\s\S]{0,400}data-testid="pair-unknown" title=\{pairError\}>\s*Pairings unavailable/,
			'the table renders a persistent status carrying the error'
		);
		const panel = readFileSync(new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url), 'utf8');
		assert.match(panel, /pairError=\{pairingGraph\.error \?\? pairingIndex\.error\}/, 'BrowserPanel wires the graph or index error through');
	});

	it('TrackTable requires pairPartners: an omitted map must not read as no pairings', () => {
		const source = readFileSync(TRACK_TABLE, 'utf8');
		assert.match(source, /\n\t\tpairPartners: ReadonlyMap<string, ReadonlySet<string>>;/);
		assert.doesNotMatch(source, /pairPartners = new Map/);
	});
});

describe('right-click "Pair these 2 tracks"', () => {
	const item = (count, ctx = TRACK_CTX, rows = count) =>
		registry.menuFor({ kind: 'track', count, rows }, ctx).find((i) => i.id === 'pair');

	it('is enabled for exactly 2 rows and names the POST route', () => {
		const it2 = item(2);
		assert.ok(it2, 'pair item must exist for a two-row selection');
		assert.equal(it2.label, 'Pair these 2 tracks');
		assert.equal(it2.enabled, true);
		assert.match(it2.title, /POST \/api\/v1\/pairings/);
	});

	it('is hidden for one row, disabled with a reason for three, hidden on a stick', () => {
		assert.equal(item(1, { ...TRACK_CTX, selection: 'one' }), undefined);
		const it3 = item(3);
		assert.equal(it3.enabled, false);
		assert.match(it3.disabledReason, /exactly 2/);
		assert.equal(item(2, { ...TRACK_CTX, source: 'usb' }), undefined);
	});

	it('counts selected rows, not distinct tracks: duplicates of A and B make three rows', () => {
		// Rows A, A, B share two stable ids. PAIR-10 pairs exactly two ROWS, so
		// this must stay disabled even though the id count is 2 (Sol P1, #6062).
		const dup = item(2, TRACK_CTX, 3);
		assert.equal(dup.enabled, false);
		assert.match(dup.disabledReason, /exactly 2/);
	});

	it('two rows of one track stay disabled: runPairTracks needs two distinct ids (Sol P1, #6062)', () => {
		const same = item(1, TRACK_CTX, 2);
		assert.equal(same.enabled, false);
		assert.match(same.disabledReason, /different tracks/);
		assert.equal(item(2, TRACK_CTX, 2).enabled, true, 'two rows of two tracks still pair');
	});
});

describe('TrackTable and BrowserPanel wiring', () => {
	const source = readFileSync(TRACK_TABLE, 'utf8');
	const ast = parse(source, { modern: true });
	const css = source.slice(ast.css.start, ast.css.end);

	it('the track row binds rb-row-pair and rb-row-pair-live to pairRowState', () => {
		const row = [...walk(ast.fragment)].find(
			(n) =>
				n.type === 'RegularElement' &&
				n.name === 'tr' &&
				n.attributes.some((a) => a.name === 'data-testid' && a.value?.[0]?.data === 'track-row')
		);
		assert.ok(row);
		for (const [name, want] of [
			['rb-row-pair', "!== 'none'"],
			['rb-row-pair-live', "=== 'live'"]
		]) {
			const d = row.attributes.find((a) => a.type === 'ClassDirective' && a.name === name);
			assert.ok(d, `track row must carry class:${name}`);
			const text = source.slice(d.expression.start, d.expression.end);
			assert.equal(text, `pairRowState(pairPartners, loadedIds, row.stable_id) ${want}`);
		}
	});

	it('the zigzag is drawn inside the row, runs one comet lap on view and hover, and stops for reduced motion', () => {
		const still = css.match(/tbody tr\.rb-row-pair::after \{[\s\S]*?\n\t\}/)[0];
		// Comet ink (62% mix) at 40%, cut to a zigzag on all four sides, inset inside the row.
		assert.match(css, /--pz-ink: color-mix\(in srgb, var\(--rb-pair\) 62%, var\(--rb-text-dim\)\)/);
		assert.match(css, /--pz-base: color-mix\(in srgb, var\(--pz-ink\) 40%, transparent\)/);
		assert.match(still, /background-color: var\(--pz-base\)/);
		assert.match(still, /mask-image: var\(--pz-h\), var\(--pz-h\), var\(--pz-v\), var\(--pz-v\)/);
		assert.match(still, /top: 1px;\s*bottom: 1px;\s*left: 1px;\s*right: 1px;/);
		assert.match(css, /--pz-h: url\("data:image\/svg\+xml,[^"]*viewBox='0 0 10 3'[^"]*polyline points='0,1\.9 5,1\.1 10,1\.9'/);
		// One finite lap, never a loop: the comet must stop once it has run.
		assert.match(still, /animation: pair-comet 0\.85s linear;/);
		assert.doesNotMatch(still, /infinite/);
		// The head layers peak at 95% and are driven by the registered envelope.
		assert.match(still, /--pz-head: color-mix\(in srgb, var\(--pz-ink\) calc\(var\(--pz-e\) \* 0\.95\), transparent\)/);
		for (const v of ['e', 't', 'r', 'b', 'l']) {
			assert.match(css, new RegExp(`@property --pz-${v} \\{\\s*syntax: '<percentage>';\\s*inherits: false;`));
		}
		// Hover APPENDS a second run, so pair-comet keeps its place and leaving
		// the row does not replay it.
		assert.match(css, /tbody tr\.rb-row-pair:hover::after \{\s*animation: pair-comet 0\.85s linear, pair-comet-again 0\.85s linear;/);
		const frames = (name) => css.match(new RegExp(`@keyframes ${name} \\{([\\s\\S]*?)\\n\\t\\}`))[1];
		assert.equal(frames('pair-comet'), frames('pair-comet-again'));
		// The envelope starts and ends dark, so a finished comet leaves nothing lit.
		assert.match(frames('pair-comet'), /0% \{[^}]*--pz-e: 0%;/);
		assert.match(frames('pair-comet'), /100% \{[^}]*--pz-e: 0%;/);
		assert.match(frames('pair-comet'), /50% \{[^}]*--pz-e: 100%;/);
		// The looping pulses stay, but only behind the flag's class.
		assert.match(css, /\.rb-pair-pulses tbody tr\.rb-row-pair-live::after \{[\s\S]*?animation: pair-pulse 1\.7s linear infinite/);
		assert.doesNotMatch(css, /\n\ttbody tr\.rb-row-pair-live::after/);
		assert.match(source, /class:rb-pair-pulses=\{pairPulses\}/);
		assert.match(source, /pairPulses = false,/);
		assert.match(
			css,
			/prefers-reduced-motion: reduce\)\s*\{\s*tbody tr\.rb-row-pair::after,\s*tbody tr\.rb-row-pair:hover::after,\s*\.rb-pair-pulses tbody tr\.rb-row-pair-live::after \{\s*animation: none;\s*background-image: none/
		);
		// The master's partners get a stronger ring, never a cell wash: a wash
		// darkened the muted time column under AA on a loaded light row.
		assert.match(css, /tbody tr\.rb-row-pair\.rb-row-paired::after \{\s*--pz-base: color-mix\(in srgb, var\(--pz-ink\) 52%, transparent\);/);
		assert.doesNotMatch(css, /rb-row-paired td/);
		// Chrome lays a row's ::before out as a leading cell, shifting columns.
		assert.doesNotMatch(css, /tr\.rb-row-pair[^{,]*::before/);
	});

	it('the pulses flag is declared off by default on the server', () => {
		const store = readFileSync(new URL('../../../../feature_flags/store.py', import.meta.url), 'utf8');
		assert.match(store, /flag_id="library\.pair_edge_pulses",\s*default=False,/);
		// Loaded only against the engine daemon, which serves /api/v1/flags;
		// the legacy daemon would 404 on every library mount.
		const graph = readFileSync(new URL('../../src/lib/rb/pairing-graph.svelte.ts', import.meta.url), 'utf8');
		assert.match(graph, /capabilities\.probe\(\)\.then\(\(flavor\) => \{\s*if \(flavor === 'engine'\) void buildFlags\.load\(\);/);
		assert.match(graph, /PAIR_PULSES_FLAG = 'library\.pair_edge_pulses'/);
		assert.match(graph, /return buildFlags\.flag\(PAIR_PULSES_FLAG\)\?\.enabled === true;/);
		const panel = readFileSync(BROWSER_PANEL, 'utf8');
		assert.match(panel, /pairPulses=\{pairingGraph\.pulses\}/);
	});

	it('the PAIR fold arrow renders from pairFold and jumps to the pair', () => {
		const btn = [...walk(ast.fragment)].find(
			(n) =>
				n.type === 'RegularElement' &&
				n.name === 'button' &&
				n.attributes.some((a) => a.name === 'data-testid' && a.value?.[0]?.data === 'pair-fold')
		);
		assert.ok(btn, 'pair-fold button must exist');
		const onclick = btn.attributes.find((a) => a.name === 'onclick');
		assert.equal(source.slice(onclick.value.expression.start, onclick.value.expression.end), 'jumpToPair');
		assert.match(source, /const pairRowIndices = \$derived\(partnerIndices\(rows, pairedPartnerIds\)\)/);
		assert.match(source, /const pairIndex = \$derived\(pairTarget\?\.index \?\? -1\)/);
		assert.match(css, /\.pair-fold \{[^}]*color: var\(--rb-pair\)/);
	});

	it('every skin defines --rb-pair', () => {
		const theme = readFileSync(THEME, 'utf8');
		const count = (theme.match(/--rb-pair:/g) ?? []).length;
		assert.ok(count >= 3, `default, Gothic and Light each need --rb-pair (found ${count})`);
		assert.match(theme, /html\[data-skin='mono-dev'\] \.perf-root \{[^}]*--rb-pair:/);
		assert.match(theme, /html\[data-skin='light'\] \.perf-root \{[^}]*--rb-pair:/);
	});

	it('BrowserPanel feeds every TrackTable pairPartners from a started PairingGraph', () => {
		const panel = readFileSync(BROWSER_PANEL, 'utf8');
		const past = parse(panel, { modern: true });
		const tables = [...walk(past.fragment)].filter(
			(n) => n.type === 'Component' && n.name === 'TrackTable'
		);
		assert.ok(tables.length >= 1);
		for (const t of tables) {
			const a = t.attributes.find((x) => x.name === 'pairPartners');
			assert.ok(a, 'TrackTable needs pairPartners');
			const e = a.value.expression;
			assert.equal(panel.slice(e.start, e.end), 'pairingGraph.partners');
		}
		assert.match(panel, /pairingGraph\.start\(\)/);
		assert.match(panel, /pairingGraph\.stop\(\)/);
		assert.match(panel, /new PairingGraph\(\)/);
		assert.match(panel, /\$effect\(\(\) => pairingOutage\(pairingGraph, pairingIndex\)\)/);
	});
});

describe('PairingGraph wiring', () => {
	const source = readFileSync(PAIRING_GRAPH, 'utf8');

	it('a pairings event is debounced into one refresh; a resync refreshes at once', () => {
		assert.match(source, /subscribeKind\('pairings', \(\) => this\.requestRefresh\(\)\)/);
		assert.match(source, /subscribeResync\(\(\) => void this\.refresh\(\)\)/);
	});

	it("the master's PairingIndex (PAIR arrow, stronger ring) also reloads on resync", () => {
		const index = readFileSync(PAIRING_INDEX, 'utf8');
		assert.match(index, /subscribeResync\(\(\) => void this\.bump\(getMasterStableId\)\)/);
	});

	it('reads the real API, and a stale answer cannot overwrite a newer one', () => {
		assert.match(source, /await listPairings\(\);\n\t\t\tif \(generation !== this\._generation\) return;/);
		assert.doesNotMatch(source, /list\?:|_list\b/, 'no injected list seam');
	});
});
