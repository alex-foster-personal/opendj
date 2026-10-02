/**
 * PAIR-04 library chrome: a row whose track is paired with the reference
 * master gets the purple `rb-row-paired` underline.
 *
 * Sol P1 on PR #4014 (r4161786218): the previous version of this file only
 * exercised JavaScript's Set. This one drives the production chain end to end
 * as far as a DOM-less suite can:
 *   1. PairingIndex (real module, real listPairingsFor, fetch stubbed at the
 *      network edge) turns a pairings response into partner ids;
 *   2. isPairedRow, the predicate TrackTable binds to the class, decides
 *      highlight per row from those ids;
 *   3. the parsed Svelte AST proves TrackTable's track row binds
 *      `rb-row-paired` to isPairedRow(pairedPartnerIds, row.stable_id), that
 *      the class has a style, and that BrowserPanel feeds the prop from
 *      pairingIndex.partnerIds.
 * Why not an SSR render of TrackTable: it virtualizes rows from a measured
 * viewport height, which is 0 without a DOM, so SSR renders no rows at all
 * and a render-based assertion could not fail for the reason under test.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, describe, it } from 'node:test';

import { parse } from 'svelte/compiler';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://pairing-indicators.example.test';
const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const TRACK_TABLE = `${SRC}/lib/components/rb/browser/TrackTable.svelte`;
const BROWSER_PANEL = `${SRC}/lib/components/rb/BrowserPanel.svelte`;

let indexMod;
let rowMod;
let originalFetch;

function pairing(id, from, to) {
	return {
		pairing_id: id,
		from_stable_id: from,
		to_stable_id: to,
		direction: '<->',
		source: 'manual',
		notes: null,
		snapshot: null,
		created_at: '2026-09-30T00:00:00+00:00',
		updated_at: '2026-09-30T00:00:00+00:00'
	};
}

function servePairings(rows) {
	globalThis.fetch = async (request) => {
		const url = new URL(request.url);
		const from = url.searchParams.get('from_stable_id');
		const to = url.searchParams.get('to_stable_id');
		const body = rows.filter((p) => (from ? p.from_stable_id === from : p.to_stable_id === to));
		return new Response(JSON.stringify(body), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
}

/** Depth-first walk over a Svelte 5 modern AST. */
function* walk(node) {
	if (node === null || typeof node !== 'object') return;
	if (typeof node.type === 'string') yield node;
	for (const [key, value] of Object.entries(node)) {
		if (key === 'parent') continue;
		if (Array.isArray(value)) for (const child of value) yield* walk(child);
		else if (value && typeof value === 'object') yield* walk(value);
	}
}

function attr(element, name) {
	return element.attributes.find((a) => a.type === 'Attribute' && a.name === name);
}

before(async () => {
	indexMod = await loadTypeScriptModule('src/lib/rb/pairing-index.svelte.ts', {
		viteApiBase: API_BASE
	});
	rowMod = await loadTypeScriptModule('src/lib/rb/pairing-row.ts');
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

describe('pairing-library-indicators', () => {
	it('a partner of the master is highlighted, other rows and the master are not', async () => {
		servePairings([
			pairing('p1', 'master', 'partner-out'),
			pairing('p2', 'partner-in', 'master'),
			pairing('p3', 'someone', 'else')
		]);
		const index = new indexMod.PairingIndex();
		await index.refresh(() => 'master');

		const highlighted = (sid) => rowMod.isPairedRow(index.partnerIds, sid);
		assert.equal(highlighted('partner-out'), true, 'master -> partner row must be highlighted');
		assert.equal(highlighted('partner-in'), true, 'partner -> master row must be highlighted');
		assert.equal(highlighted('someone'), false, 'an unrelated pairing must not highlight');
		assert.equal(highlighted('master'), false, 'the master row itself is not its own partner');
	});

	it('no master means no highlighted rows', async () => {
		servePairings([pairing('p1', 'master', 'partner-out')]);
		const index = new indexMod.PairingIndex();
		await index.refresh(() => 'master');
		assert.equal(rowMod.isPairedRow(index.partnerIds, 'partner-out'), true);
		await index.refresh(() => null);
		assert.equal(rowMod.isPairedRow(index.partnerIds, 'partner-out'), false);
	});

	it("TrackTable's track row binds rb-row-paired to isPairedRow over the prop", () => {
		const source = readFileSync(TRACK_TABLE, 'utf8');
		const ast = parse(source, { modern: true });
		const rows = [...walk(ast.fragment)].filter(
			(n) =>
				n.type === 'RegularElement' &&
				n.name === 'tr' &&
				attr(n, 'data-testid')?.value?.[0]?.data === 'track-row'
		);
		assert.equal(rows.length, 1, 'expected exactly one track-row <tr> in TrackTable');
		const directive = rows[0].attributes.find(
			(a) => a.type === 'ClassDirective' && a.name === 'rb-row-paired'
		);
		assert.ok(directive, 'track row must carry class:rb-row-paired');
		const expr = directive.expression;
		assert.equal(expr.type, 'CallExpression');
		assert.equal(expr.callee.name, 'isPairedRow');
		assert.equal(expr.arguments[0].name, 'pairedPartnerIds');
		assert.equal(source.slice(expr.arguments[1].start, expr.arguments[1].end), 'row.stable_id');

		const css = source.slice(ast.css.start, ast.css.end);
		assert.match(css, /\.rb-row-paired\b[^{]*\{/, 'rb-row-paired needs a style rule to be visible');
	});

	it('BrowserPanel feeds TrackTable pairedPartnerIds from its PairingIndex', () => {
		const source = readFileSync(BROWSER_PANEL, 'utf8');
		const ast = parse(source, { modern: true });
		const tables = [...walk(ast.fragment)].filter(
			(n) => n.type === 'Component' && n.name === 'TrackTable'
		);
		assert.ok(tables.length >= 1, 'BrowserPanel must render TrackTable');
		const wired = tables.filter((t) => {
			const a = attr(t, 'pairedPartnerIds');
			const e = a?.value?.expression;
			return e && source.slice(e.start, e.end) === 'pairingIndex.partnerIds';
		});
		assert.equal(wired.length, tables.length, 'every TrackTable needs pairedPartnerIds wired');
		const scripts = [ast.instance].map((s) => source.slice(s.start, s.end)).join('\n');
		assert.match(scripts, /const pairingIndex = new PairingIndex\(/);
		// A failed lookup must reach the operator as an error toast (Sol P1, r4161786229).
		assert.match(
			scripts,
			/new PairingIndex\(\{\s*onError: \(message\) => pushToast\(message, 'error'\)/
		);
	});
});
