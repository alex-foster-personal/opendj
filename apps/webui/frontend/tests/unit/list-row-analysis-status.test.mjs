/**
 * STANDALONE-03: All Tracks list rows carry analysis status/reason.
 *
 * [if] a list wire has bpm_status missing [then] rowFromListWire keeps it, [else stop].
 *
 * Mutation check: drop the bpm_status copy in rowFromListWire and this goes red.
 * Before the fix the mapper never copied status fields, so empty All Tracks
 * cells had undefined bpm_status and the table treated them as ordinary blanks.
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let wire;
before(async () => {
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
});

const baseListWire = {
	stable_id: 'a'.repeat(40),
	title: 'Empty analysis',
	artist: null,
	album: null,
	duration_ms: 60_000,
	bpm: null,
	key: null,
	rating: null,
	tags: [],
	notes: null,
	created_at: '2026-10-08T00:00:00Z',
	updated_at: '2026-10-08T00:00:00Z',
	provenance: {},
	has_rb_mapping: false,
	lyrics_available: false,
	auto_cues_available: false,
	stems_available: false,
	artwork_available: false,
	artwork_status: 'no_image_path',
	preview_b64: null,
	preview_max: null,
	file_availability: 'present',
	file_exists: true,
	is_streaming: false,
	has_remote_copy: false,
	cloud_transfer: null,
	quality: {
		venue: 'lounge',
		label: 'Lounge',
		rank: 1,
		of: 6,
		blurb: 'test',
		kbps: 128,
		container: '.mp3',
		lossless: false
	},
	play_count: 0,
	vocals: { status: 'not_analyzed' },
	stems: { status: 'none' },
	energy: null,
	energy_source: null,
	energy_reason: 'no Mixed In Key energy has been imported',
	bpm_status: 'missing',
	bpm_reason: 'no own analysis record for this lane yet',
	key_status: 'missing',
	key_reason: 'no own analysis record for this lane yet',
	loudness_status: 'missing',
	loudness_reason: 'no own analysis record for this lane yet'
};

describe('rowFromListWire keeps analysis status for empty cells', () => {
	it('copies bpm/key/loudness status and reason from the list wire', () => {
		const row = wire.rowFromListWire(baseListWire, 1);
		assert.equal(row.bpm_status, 'missing');
		assert.equal(row.bpm_reason, 'no own analysis record for this lane yet');
		assert.equal(row.key_status, 'missing');
		assert.equal(row.key_reason, 'no own analysis record for this lane yet');
		assert.equal(row.loudness_status, 'missing');
		assert.equal(row.loudness_reason, 'no own analysis record for this lane yet');
	});

	it('fills the stock missing sentence when the wire omits it', () => {
		const omitted = { ...baseListWire };
		delete omitted.bpm_reason;
		const row = wire.rowFromListWire(omitted, 1);
		assert.equal(row.bpm_status, 'missing');
		assert.equal(row.bpm_reason, 'bpm not analyzed');
	});

	it('reads an absent status as ok only beside a served value', () => {
		const compacted = { ...baseListWire, bpm: 128, key: '8A' };
		for (const field of ['bpm_status', 'bpm_reason', 'key_status', 'key_reason']) {
			delete compacted[field];
		}
		const row = wire.rowFromListWire(compacted, 1);
		assert.equal(row.bpm_status, 'ok');
		assert.equal(row.bpm_reason, null);
		assert.equal(row.key_status, 'ok');
		assert.equal(row.key_reason, null);
	});

	// Mutation check: return `status ?? "ok"` from listingLaneStatus and this
	// goes red. An empty cell with no status is a dropped field, not a success.
	it('reports an absent status beside an empty value as failed with a reason', () => {
		const dropped = { ...baseListWire };
		for (const lane of ['bpm', 'key', 'loudness']) {
			delete dropped[`${lane}_status`];
			delete dropped[`${lane}_reason`];
		}
		const row = wire.rowFromListWire(dropped, 1);
		assert.equal(row.bpm_status, 'failed');
		assert.equal(row.bpm_reason, 'server sent no bpm status for an empty value');
		assert.equal(row.key_status, 'failed');
		assert.equal(row.key_reason, 'server sent no key status for an empty value');
		assert.equal(row.loudness_status, 'failed');
		assert.equal(row.loudness_reason, 'server sent no loudness status for an empty value');
	});

	it('keeps an explicit ok status even when the value is empty', () => {
		const row = wire.rowFromListWire(
			{ ...baseListWire, bpm_status: 'ok', bpm_reason: null, loudness_status: 'ok' },
			1
		);
		assert.equal(row.bpm_status, 'ok');
		assert.equal(row.bpm_reason, null);
		assert.equal(row.loudness_status, 'ok');
	});

	// The server keeps an explicit ok beside an empty string (same predicate
	// as the client), so that row arrives with its status and stays ok.
	it('keeps the served ok on an empty-string key row', () => {
		const row = wire.rowFromListWire({ ...baseListWire, key: '', key_status: 'ok', key_reason: null }, 1);
		assert.equal(row.key_status, 'ok');
		assert.equal(row.key_reason, null);
	});

	it('treats an empty-string value with no status as no value, not as ok', () => {
		const dropped = { ...baseListWire, key: '' };
		delete dropped.key_status;
		delete dropped.key_reason;
		const row = wire.rowFromListWire(dropped, 1);
		assert.equal(row.key_status, 'failed');
		assert.equal(row.key_reason, 'server sent no key status for an empty value');
	});

	// LIBUX-34. Mutation check: drop the bpm_method copy in rowFromListWire
	// and this goes red. The API half is (private path)
	// and the hover half is bpm-cell-title.test.mjs.
	it('carries beatgrid provenance from the list wire onto the row', () => {
		const analyzed = {
			...baseListWire,
			bpm: 128,
			bpm_source: 'own_beatgrid',
			bpm_method: 'manual',
			bpm_confidence: 0.9
		};
		delete analyzed.bpm_status;
		delete analyzed.bpm_reason;
		const row = wire.rowFromListWire(analyzed, 1);
		assert.equal(row.bpm_status, 'ok');
		assert.equal(row.bpm_source, 'own_beatgrid');
		assert.equal(row.bpm_method, 'manual');
		assert.equal(row.bpm_confidence, 0.9);
	});
});
