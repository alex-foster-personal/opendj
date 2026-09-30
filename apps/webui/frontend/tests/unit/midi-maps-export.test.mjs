// requirement: NAE-17
// The Rust audio engine (apps/audio-engine, plan 20-03) binds controllers
// with the page's own device maps, exported to
// apps/audio-engine/maps/device-maps.json by scripts/export-midi-maps.mjs.
// This keeps that export in step with the TypeScript maps.
//
// Regression lines (single-line format per CLAUDE.md):
//   if a TS device map changes without rerunning the export then broken
//   if the export drops a registry map or reorders them then broken

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { EXPORT_PATH, renderExport } from '../../scripts/export-midi-maps.mjs';

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
		['DDJ-FLX10', 'DDJ-400', '\\bMixtour\\s+Pro\\b', '\\bMixtour\\b(?!\\s+Pro\\b)', 'DDJ-FLX4']
	);
	for (const m of doc.maps) assert.ok(m.bindings.length > 0, `${m.nameMatch} has no bindings`);
});
