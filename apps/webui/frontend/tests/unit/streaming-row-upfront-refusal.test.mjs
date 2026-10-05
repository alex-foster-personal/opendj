/**
 * Issue #3934: All Tracks rows learned they were streaming only after a load
 * attempt. `TrackListItemOut` carries no `is_streaming`, so the list mapper
 * started every row at null and the real flag arrived later via rb-meta. In
 * between, a streaming row (file_exists false) was refused as a "missing on
 * disk (broken link)" row, and hide-broken hid it as broken. The server
 * already names a streaming URI `file_availability: 'streaming'`, so the row
 * is now settled as streaming the moment it is mapped.
 *
 * Regression lines:
 * - if a streaming list row maps with is_streaming null then its first load
 *   or drag reads as a broken link, and only a later attempt says streaming
 * - if a present row maps as streaming then a playable track is refused
 * - if the refusal ignores file_availability then a streaming row is still
 *   called a broken link before rb-meta hydrates
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STREAMING_REFUSAL = "Streaming track: Open DJ can't play streaming services";
const BROKEN_REFUSAL = 'cannot load: audio file missing on disk (broken link)';

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
	// A captured real server row, reshaped onto the list endpoint's fields.
	const row = JSON.parse(readFileSync(fixture, 'utf8')).find((r) => r.file_availability === 'present');
	const { is_streaming: _drop, comments: _comments, ...rest } = row;
	base = { ...rest, notes: null };
});

const listRow = (overrides) => wire.rowFromListWire({ ...base, ...overrides }, 1);

describe('a streaming All Tracks row is streaming from the moment it is mapped', () => {
	it('maps file_availability streaming onto is_streaming true', () => {
		const row = listRow({ file_availability: 'streaming', file_exists: false });
		assert.equal(row.is_streaming, true);
		assert.equal(row.file_exists, false);
	});

	it('is refused up front with the streaming reason, never as a broken link', () => {
		const row = listRow({ file_availability: 'streaming', file_exists: false });
		assert.equal(refusal.trackDragRefusal(row), STREAMING_REFUSAL);
	});

	it('hides under hide-broken and is not counted locally available', () => {
		const row = listRow({ file_availability: 'streaming', file_exists: false });
		assert.equal(wire.rowRendersUnavailable(row), true);
		assert.deepEqual(contract.filterRows([row], '', true), []);
		assert.equal(contract.rowIsLocallyAvailable(row), false);
	});

	it('a really missing row is still a broken link, not streaming', () => {
		const row = listRow({ file_availability: 'absent', file_exists: false });
		assert.equal(row.is_streaming, null);
		assert.equal(refusal.trackDragRefusal(row), BROKEN_REFUSAL);
		assert.deepEqual(contract.filterRows([row], '', true), []);
	});

	it('a present row is not marked streaming and still loads', () => {
		const row = listRow({ file_availability: 'present', file_exists: true });
		assert.equal(row.is_streaming, null, 'unknown stays null so the rb-meta fallback still applies');
		assert.equal(refusal.trackDragRefusal(row), null);
		assert.equal(contract.rowIsLocallyAvailable(row), true);
	});
});

describe('listRowIsStreaming', () => {
	it('answers true for streaming and a named scheme status', () => {
		assert.equal(wire.listRowIsStreaming('streaming'), true);
		assert.equal(wire.listRowIsStreaming('tidal-streaming'), true);
		for (const status of ['present', 'absent', 'awaiting_volume', 'AVAILABILITY_PENDING', null, undefined]) {
			assert.equal(wire.listRowIsStreaming(status), null, String(status));
		}
	});
});

describe('trackDragRefusal reads the availability status too', () => {
	it('refuses an unhydrated streaming row as streaming', () => {
		assert.equal(
			refusal.trackDragRefusal({
				file_exists: false,
				file_availability: 'streaming',
				is_streaming: false
			}),
			STREAMING_REFUSAL
		);
	});
});
