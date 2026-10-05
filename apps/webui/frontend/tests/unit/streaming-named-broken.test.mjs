/**
 * LIBM-167: a streaming-service URI is a named broken link.
 *
 * [if] a tidal URI row is listed [then] its label is tidal-streaming, the
 *   row is broken, Broken-unticked hides it, and load is refused with
 *   "Tidal streaming track: Open DJ can't play streaming services" [else stop]
 * [if] the local file is on disk [then] the row stays present and can load
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const TIDAL = "Tidal streaming track: Open DJ can't play streaming services";

let wire;
let contract;
let refusal;
let base;
before(async () => {
	wire = await loadTypeScriptModule('src/lib/components/rb/browser/browser-row-wire.ts');
	contract = await loadTypeScriptModule('src/lib/components/rb/browser/pane-contract.svelte.ts');
	refusal = await loadTypeScriptModule('src/lib/rb/track-drag-refusal.ts');
	const fixture = fileURLToPath(
		new URL('./fixtures/playlist-pending-availability-captured.json', import.meta.url)
	);
	const row = JSON.parse(readFileSync(fixture, 'utf8')).find((r) => r.file_availability === 'present');
	const { is_streaming: _drop, comments: _comments, ...rest } = row;
	base = { ...rest, notes: null };
});

const listRow = (overrides) => wire.rowFromListWire({ ...base, ...overrides }, 1);

describe('a tidal streaming URI is a named broken link', () => {
	it('labels, greys, filters and refuses the tidal row', () => {
		const row = listRow({
			file_availability: 'streaming',
			file_exists: false,
			file_path: 'tidal:tracks:99560085',
			streaming_provider: 'tidal'
		});
		assert.equal(row.file_availability, 'tidal-streaming');
		assert.equal(row.is_streaming, true);
		assert.equal(wire.rowRendersUnavailable(row), true);
		assert.equal(wire.libraryRowHoverTitle(row), TIDAL);
		assert.equal(wire.libraryAudioLoadRefusal(row), TIDAL);
		assert.equal(refusal.trackDragRefusal(row), TIDAL);
		assert.deepEqual(contract.filterRows([row], '', true), []);
		assert.deepEqual(contract.filterRows([row], '', false), [row]);
	});

	it('keeps a legacy streaming value refused when the path has no scheme', () => {
		const row = {
			file_exists: false,
			file_availability: 'streaming',
			is_streaming: true
		};
		assert.equal(wire.rowRendersUnavailable(row), true);
		assert.equal(
			wire.libraryAudioLoadRefusal(row),
			"Streaming track: Open DJ can't play streaming services"
		);
	});

	it('names beatport from the URI even when the wire still says absent', () => {
		const row = listRow({
			file_availability: 'absent',
			file_exists: false,
			file_path: 'beatport:tracks:42'
		});
		assert.equal(row.file_availability, 'beatport-streaming');
		assert.equal(
			wire.libraryAudioLoadRefusal(row),
			"Beatport streaming track: Open DJ can't play streaming services"
		);
		assert.deepEqual(contract.filterRows([row], '', true), []);
	});

	it('leaves a present local row playable (control)', () => {
		const row = listRow({
			file_availability: 'present',
			file_exists: true,
			file_path: '/music/here.wav'
		});
		assert.equal(row.file_availability, 'present');
		assert.equal(row.is_streaming, null);
		assert.equal(wire.rowRendersUnavailable(row), false);
		assert.equal(wire.libraryAudioLoadRefusal(row), null);
		assert.equal(refusal.trackDragRefusal(row), null);
		assert.deepEqual(contract.filterRows([row], '', true), [row]);
		assert.equal(wire.libraryRowHoverTitle(row), undefined);
	});
});
