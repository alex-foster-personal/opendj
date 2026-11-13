import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 },
	{ n: 1, bpm: 127, t: 2.025 },
	{ n: 2, bpm: 127, t: 2.497 },
	{ n: 3, bpm: 127, t: 2.97 },
	{ n: 4, bpm: 127, t: 3.442 }
];

const NONUNIFORM_PQTZ = [
	{ n: 1, bpm: 120, t: 0.0 },
	{ n: 2, bpm: 120, t: 0.35 },
	{ n: 3, bpm: 120, t: 1.1 },
	{ n: 4, bpm: 120, t: 1.45 },
	{ n: 1, bpm: 128, t: 2.6 }
];

const WARP_MAP_MANIFEST = new URL(
	'./fixtures/warp-map-pqtz-captured.manifest.json',
	import.meta.url
);
const WARP_MAP_CAPTURE = new URL('./fixtures/warp-map-pqtz-captured.json', import.meta.url);

function sha256Hex(text) {
	return createHash('sha256').update(text).digest('hex');
}

test('[if] real PQTZ nonuniform intervals [then] map beats and round-trip without changing source', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/warp-map.ts');
	const built = mod.buildWarpMapFromPqtz(NONUNIFORM_PQTZ);
	const intervals = mod.warpMapIntervals(built);
	assert.equal(intervals.length, NONUNIFORM_PQTZ.length - 1);
	assert.ok(
		intervals.some((span) => span.source_s1 - span.source_s0 > 0.5),
		'expected at least one nonuniform source interval'
	);
	for (let index = 0; index < NONUNIFORM_PQTZ.length; index++) {
		assert.equal(built.anchors[index].source_s, NONUNIFORM_PQTZ[index].t);
		assert.equal(built.anchors[index].beat, index);
	}
	const roundTrip = mod.parseWarpMap(mod.serializeWarpMap(built));
	assert.deepEqual(roundTrip.anchors, built.anchors);
});

test('[if] duplicate or non-monotonic anchor coordinates [then] validation fails before schedule', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/warp-map.ts');
	const bad = {
		v: 1,
		anchors: [
			{ source_s: 0, beat: 0, kind: 'analyzed' },
			{ source_s: 1, beat: 1, kind: 'analyzed' },
			{ source_s: 1, beat: 2, kind: 'locked' }
		]
	};
	assert.throws(() => mod.assertWarpMapSchedulable(bad), mod.WarpMapValidationError);
	assert.throws(
		() =>
			mod.assertWarpMapSchedulable({
				v: 1,
				anchors: [
					{ source_s: 0, beat: 0, kind: 'analyzed' },
					{ source_s: 1, beat: 1, kind: 'analyzed' },
					{ source_s: 2, beat: 1, kind: 'creative' }
				]
			}),
		mod.WarpMapValidationError
	);
	assert.throws(
		() =>
			mod.assertWarpMapSchedulable({
				v: 1,
				anchors: [
					{ source_s: Number.NaN, beat: 0, kind: 'analyzed' },
					{ source_s: 1, beat: 1, kind: 'analyzed' }
				]
			}),
		mod.WarpMapValidationError
	);
});

test('[if] analysis absent or invalid [then] no fallback beat grid is synthesized', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/warp-map.ts');
	assert.throws(() => mod.buildWarpMapFromPqtz([{ n: 1, bpm: 120, t: 0 }]), /at least 2 beats/);
	assert.throws(
		() => mod.buildWarpMapFromBeatgrid({ source: 'rekordbox', beat_count: 0, beats: [] }),
		/no fallback/
	);
	assert.throws(
		() =>
			mod.buildWarpMapFromBeatgrid({
				source: 'own',
				status: 'missing',
				beat_count: 0,
				beats: []
			}),
		/no fallback/
	);
});

test('[if] analyzed locked and creative anchors coexist [then] each kind persists through reload', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/warp-map.ts');
	const mixed = mod.parseWarpMap(
		JSON.stringify({
			v: 1,
			anchors: [
				{ source_s: 0, beat: 0, kind: 'analyzed' },
				{ source_s: 0.5, beat: 1, kind: 'locked' },
				{ source_s: 1.2, beat: 2, kind: 'creative' },
				{ source_s: 1.8, beat: 3, kind: 'analyzed' }
			]
		})
	);
	assert.deepEqual(
		mixed.anchors.map((anchor) => anchor.kind),
		['analyzed', 'locked', 'creative', 'analyzed']
	);
	const serialized = mod.serializeWarpMap(mixed);
	const reloaded = mod.parseWarpMap(serialized);
	assert.deepEqual(reloaded.anchors, mixed.anchors);
});

/** Verify the captured PQTZ warp-map fixture bytes and SHA-256 against its manifest, then round-trip it. */
test('[if] captured PQTZ warp-map fixture [then] bytes and sha256 match manifest and it round-trips', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/warp-map.ts');
	const manifest = JSON.parse(readFileSync(WARP_MAP_MANIFEST, 'utf8'));
	assert.equal(manifest.schema_version, mod.WARP_MAP_FIXTURE_MANIFEST_SCHEMA);
	const entry = manifest.files['warp-map-pqtz-captured.json'];
	assert.ok(entry, 'manifest needs an entry for the captured fixture');
	const raw = readFileSync(WARP_MAP_CAPTURE);
	assert.equal(raw.length, entry.bytes, 'captured fixture byte count differs from manifest');
	assert.equal(
		createHash('sha256').update(raw).digest('hex'),
		entry.sha256,
		'captured fixture sha256 differs from manifest'
	);
	const capture = mod.parseWarpMap(raw.toString('utf8'));
	assert.equal(capture.anchors.length, REAL_PQTZ_BEATS.length);
	const seeded = mod.buildWarpMapFromPqtz(REAL_PQTZ_BEATS);
	assert.equal(
		sha256Hex(mod.warpMapChecksumInput(capture)),
		sha256Hex(mod.warpMapChecksumInput(seeded)),
		'captured map must match the map built from the real PQTZ beats'
	);
	assert.deepEqual(mod.parseWarpMap(mod.serializeWarpMap(capture)).anchors, capture.anchors);
});
