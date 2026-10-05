// requirement: NAE-17
// The Rust audio engine (apps/audio-engine, plan 20-03) binds controllers
// with the page's own device maps, exported to
// apps/audio-engine/maps/device-maps.json by scripts/export-midi-maps.mjs.
// This keeps that export in step with the TypeScript maps.
//
// Regression lines (single-line format per CLAUDE.md):
//   if a TS device map changes without rerunning the export then broken
//   if the export drops a registry map or reorders them then broken
//   if an exported nameMatch uses look-around, a backreference or \\p{..} then broken
//   if a Mixtour Pro port resolves to the classic map, or the reverse, then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { test } from 'node:test';

import { EXPORT_PATH, loadRegistry, renderExport } from '../../scripts/export-midi-maps.mjs';

const PRO = '\\bMixtour\\s+Pro\\b';
const CLASSIC = '\\bMixtour\\b(?:$|\\S|\\s+(?:$|[^\\sP]|P(?:$|[^r])|Pr(?:$|[^o])|Pro\\w))';
// Port names and the map each resolves to, shared with the Rust engine's own
// tests (apps/audio-engine/src/midi.rs) so both matchers answer one list.
const NAME_MATCH_CASES = resolve(dirname(EXPORT_PATH), '../tests/fixtures/name-match-cases.json');

/** Which constructs in a RegExp source the engine's regex_lite refuses though
 * the page's RegExp accepts them. The engine's own test compiles every
 * exported pattern for real; this is the same guard on the side that authors
 * the maps, so it fails before a stale export is even regenerated. */
function engineRejects(source) {
	const bare = source.replace(/\\\\/g, '').replace(/\\[()]/g, '');
	const found = [];
	if (/\(\?<?[=!]/.test(bare)) found.push('look-around');
	if (/\\[1-9]|\\k</.test(bare)) found.push('backreference');
	if (/\\[pP]\{/.test(bare)) found.push('unicode class');
	return found;
}

test('the committed engine export matches the page device maps byte for byte', async () => {
	const want = await renderExport();
	const have = readFileSync(EXPORT_PATH, 'utf8');
	assert.equal(
		have,
		want,
		'apps/audio-engine/maps/device-maps.json is stale: run node scripts/export-midi-maps.mjs in apps/webui/frontend'
	);
});

test('the export holds every registry map, in registry order', async () => {
	const doc = JSON.parse(await renderExport());
	// Positive control on content: the five maps the page registers, each with
	// bindings, so an empty or partial registry cannot pass as "in sync".
	assert.deepEqual(
		doc.maps.map((m) => m.nameMatch),
		['DDJ-FLX10', 'DDJ-400', PRO, CLASSIC, 'DDJ-FLX4']
	);
	for (const m of doc.maps) assert.ok(m.bindings.length > 0, `${m.nameMatch} has no bindings`);
});

test('no exported nameMatch uses syntax the engine matcher rejects', async () => {
	const doc = JSON.parse(await renderExport());
	assert.ok(doc.maps.length >= 5, `expected every registry map, found ${doc.maps.length}`);
	for (const m of doc.maps) {
		assert.deepEqual(
			engineRejects(m.nameMatch),
			[],
			`${m.vendor} nameMatch ${m.nameMatch} loads in the page but not in the Rust engine (regex_lite)`
		);
	}
	// Controls: the guard fires on each construct, including the lookahead
	// that shipped, and an escaped look-alike is not mistaken for one.
	assert.deepEqual(engineRejects('\\bMixtour\\b(?!\\s+Pro\\b)'), ['look-around']);
	assert.deepEqual(engineRejects('(?<=DDJ-)FLX4'), ['look-around']);
	assert.deepEqual(engineRejects('(DDJ)-\\1'), ['backreference']);
	assert.deepEqual(engineRejects('(?<v>DDJ)-\\k<v>'), ['backreference']);
	assert.deepEqual(engineRejects('\\p{Lu}IXTOUR'), ['unicode class']);
	assert.deepEqual(engineRejects('Mixtour \\(?!\\)'), []);
	assert.deepEqual(engineRejects('(?<v>DDJ)-(?:FLX4|400)'), []);
});

test('classic Mixtour and Mixtour Pro ports resolve as the engine resolves them, in either map order', async () => {
	const { cases } = JSON.parse(readFileSync(NAME_MATCH_CASES, 'utf8'));
	for (const want of ['classic', 'pro', 'none']) {
		assert.ok(cases.some((c) => c.want === want), `the fixture holds no ${want} case`);
	}
	const { DEVICE_MAP_REGISTRY, webmidi } = await loadRegistry();
	const kindOf = { [CLASSIC]: 'classic', [PRO]: 'pro' };
	const classic = new RegExp(CLASSIC, 'i');
	const pro = new RegExp(PRO, 'i');
	for (const order of [DEVICE_MAP_REGISTRY, [...DEVICE_MAP_REGISTRY].reverse()]) {
		webmidi._resetMidiForTests();
		for (const map of order) webmidi.registerDeviceMap(map);
		for (const { port, want } of cases) {
			const got = webmidi.resolveMapForPort(port);
			const kind = got === null ? 'none' : kindOf[got.nameMatch];
			assert.equal(kind, want, `${JSON.stringify(port)} with ${order[0].nameMatch} registered first`);
			// Each pattern on its own, in both directions.
			assert.equal(classic.test(port), want === 'classic', `classic pattern alone, ${JSON.stringify(port)}`);
			assert.equal(pro.test(port), want === 'pro', `Pro pattern alone, ${JSON.stringify(port)}`);
		}
	}
	webmidi._resetMidiForTests();
});
