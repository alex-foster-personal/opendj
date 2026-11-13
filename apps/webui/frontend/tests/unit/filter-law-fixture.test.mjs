// requirement: FXFILT-14693
// [if] filterParamsFromKnob drifts from apps/audio-engine/tests/fixtures/filter-law.json by more than 1e-9 on any field [then] this test fails
// [if] the fixture loses a grid position or a dead-zone edge [then] this test fails
// The fixture is generated here: FILTER_LAW_FIXTURE_REWRITE=1 rewrites it from the TS law (then commit it); the Rust
// mirror `filter_law_matches_the_shared_fixture` in apps/audio-engine/src/mixer.rs reads the same file.
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FIXTURE = fileURLToPath(
	new URL('../../../../audio-engine/tests/fixtures/filter-law.json', import.meta.url)
);
const TOLERANCE = 1e-9;
const COLUMNS = ['knob', 'lpHz', 'hpHz', 'dryGain', 'lpWetGain', 'hpWetGain'];

let guards;
let constants;

before(async () => {
	guards = await loadTypeScriptModule('src/lib/rb/audio-engine-guards.ts');
	constants = await loadTypeScriptModule('src/lib/player/constants.ts');
});

/** The two last bypass positions, one step inside each dead-zone edge (0.49 and 0.51 are the first filtered ones). */
function deadZoneInnerEdges() {
	const half = constants.FILTER_DEADZONE_FRAC / 2;
	return [0.5 - half + 1e-4, 0.5 + half - 1e-4];
}

/** Every fixture knob: the 101 grid positions 0, 0.01 .. 1 plus both dead-zone inner edges, ascending. */
function fixtureKnobs() {
	const grid = Array.from({ length: 101 }, (_, i) => i / 100);
	return [...grid, ...deadZoneInnerEdges()].sort((a, b) => a - b);
}

/** One fixture row from the TS law, in COLUMNS order. */
function lawRow(knob) {
	const p = guards.filterParamsFromKnob(knob);
	return [knob, p.lpHz, p.hpHz, p.dryGain, p.lpWetGain, p.hpWetGain];
}

/** Writes the fixture from the TS law, one row per line so a law change diffs per position. */
function writeFixture() {
	const head = {
		about:
			'FILTER knob law (filterParamsFromKnob), written by apps/webui/frontend/tests/unit/filter-law-fixture.test.mjs with FILTER_LAW_FIXTURE_REWRITE=1 (issue #9791); mixer.rs filter_params must match it',
		tolerance: TOLERANCE,
		deadzone_inner_edges: deadZoneInnerEdges(),
		columns: COLUMNS
	};
	const body = JSON.stringify(head, null, 1).slice(0, -2);
	const rows = fixtureKnobs().map((k) => `  ${JSON.stringify(lawRow(k))}`);
	writeFileSync(FIXTURE, `${body},\n "rows": [\n${rows.join(',\n')}\n ]\n}\n`);
}

test('[if] any fixture field differs from filterParamsFromKnob by more than 1e-9 [then] fail, [else stop].', () => {
	if (process.env.FILTER_LAW_FIXTURE_REWRITE === '1') writeFixture();
	const doc = JSON.parse(readFileSync(FIXTURE, 'utf8'));
	assert.deepEqual(doc.columns, COLUMNS);
	assert.equal(doc.tolerance, TOLERANCE);
	for (const row of doc.rows) {
		const want = lawRow(row[0]);
		for (let c = 1; c < COLUMNS.length; c++) {
			const off = Math.abs(row[c] - want[c]);
			assert.ok(
				off <= TOLERANCE,
				`knob ${row[0]} ${COLUMNS[c]}: fixture ${row[c]}, law ${want[c]}`
			);
		}
	}
});

test('[if] the fixture lacks a grid position or a dead-zone edge, or an edge is not a dry bypass [then] fail, [else stop].', () => {
	const doc = JSON.parse(readFileSync(FIXTURE, 'utf8'));
	assert.deepEqual(
		doc.rows.map((r) => r[0]),
		fixtureKnobs()
	);
	assert.deepEqual(doc.deadzone_inner_edges, deadZoneInnerEdges());
	for (const edge of doc.deadzone_inner_edges) {
		const row = doc.rows.find((r) => r[0] === edge);
		assert.deepEqual(row.slice(3), [1, 0, 0], `knob ${edge} sits inside the dead zone: dry bypass`);
	}
	for (const first of [0.49, 0.51]) {
		const row = doc.rows.find((r) => r[0] === first);
		assert.equal(row[3], 0, `knob ${first} is the first filtered position past the edge`);
	}
});
