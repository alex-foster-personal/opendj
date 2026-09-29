/**
 * Issue #2451 / STATE-10: deck drop uses the same file_exists/is_streaming
 * guards as click-to-load.
 *
 * - [if] a broken-link track is dragged onto a deck [then] the same "audio
 *   file missing on disk" message appears as when double-clicking it
 * - [if] a streaming track is dragged onto a deck [then] the same "deck load
 *   not implemented" message appears as when double-clicking it
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let applyDeckTrackDrop;

before(async () => {
	({ applyDeckTrackDrop } = await loadTypeScriptModule('src/lib/rb/deck-track-drop.ts'));
});

function runDrop({ row, occupied = false, dispatchImpl, stableId = 'SID-1' }) {
	const dispatches = [];
	const toasts = [];
	const dispatch =
		dispatchImpl ??
		(async (command) => {
			dispatches.push(command);
		});
	return applyDeckTrackDrop({
		deckId: 1,
		occupied,
		stableId,
		row,
		dispatch,
		toast: (message, kind) => toasts.push({ message, kind })
	}).then(() => ({ dispatches, toasts }));
}

describe('applyDeckTrackDrop', () => {
	it('refuses a broken link with the same toast as click-to-load', async () => {
		const { dispatches, toasts } = await runDrop({
			row: { file_exists: false, is_streaming: false }
		});
		assert.equal(toasts.length, 1);
		assert.equal(toasts[0].kind, 'error');
		assert.equal(
			toasts[0].message,
			'cannot load: audio file missing on disk (broken link)'
		);
		assert.equal(dispatches.length, 0);
	});

	it('refuses a streaming row with the same toast as click-to-load', async () => {
		const { dispatches, toasts } = await runDrop({
			row: { file_exists: true, is_streaming: true }
		});
		assert.equal(toasts.length, 1);
		assert.equal(
			toasts[0].message,
			'streaming track - deck load not implemented (see PARITY-TODO)'
		);
		assert.equal(dispatches.length, 0);
	});

	it('prefers the streaming reason when both flags block load', async () => {
		const { toasts } = await runDrop({
			row: { file_exists: false, is_streaming: true }
		});
		assert.match(toasts[0].message, /streaming/i);
		assert.doesNotMatch(toasts[0].message, /broken link/);
	});

	it('notes the target deck before dispatching load', async () => {
		assert.match(
			await readFile('src/lib/rb/deck-track-drop.ts', 'utf8'),
			/noteRecentDeck\(args\.deckId\)/
		);
	});

	it('loads a loadable row onto an empty deck', async () => {
		const { dispatches, toasts } = await runDrop({
			row: { file_exists: true, is_streaming: false }
		});
		assert.equal(toasts.length, 0);
		assert.deepEqual(dispatches, [{ type: 'load', deck: 1, stable_id: 'SID-1' }]);
	});

	it('unloads then loads when the deck is occupied', async () => {
		const { dispatches, toasts } = await runDrop({
			row: { file_exists: true, is_streaming: false },
			occupied: true
		});
		assert.equal(toasts.length, 0);
		assert.deepEqual(dispatches, [
			{ type: 'unload', deck: 1 },
			{ type: 'load', deck: 1, stable_id: 'SID-1' }
		]);
	});

	it('wraps engine errors when row flags are missing', async () => {
		const { dispatches, toasts } = await runDrop({
			row: null,
			dispatchImpl: async (command) => {
				if (command.type === 'load') throw new Error('engine boom');
			}
		});
		assert.equal(dispatches.length, 0);
		assert.equal(toasts.length, 1);
		assert.equal(toasts[0].message, 'drop load failed: engine boom');
	});

	it('still loads when row flags are missing', async () => {
		const { dispatches, toasts } = await runDrop({ row: null });
		assert.equal(toasts.length, 0);
		assert.deepEqual(dispatches, [{ type: 'load', deck: 1, stable_id: 'SID-1' }]);
	});
});

// DECKUX-21 (pin 87241ca36b61): a row dropped on a deck loads there and makes
// that deck the Space play/pause target, so Space never has to find it by scroll.
describe('DECKUX-21 drop sets the Space target deck', () => {
	let entry;
	before(async () => {
		entry = await loadTypeScriptModule('tests/unit/fixtures/deck-drop-space-target-entry.ts');
	});

	function drop(deckId, row) {
		return entry.applyDeckTrackDrop({
			deckId,
			occupied: false,
			stableId: 'SID-7',
			row,
			dispatch: async () => {},
			toast: () => {}
		});
	}

	it('a loadable drop on deck 3 makes deck 3 the Space target', async () => {
		entry.noteRecentDeck(1);
		await drop(3, { file_exists: true, is_streaming: false });
		assert.equal(entry.getRecentDeck(), 3);
	});

	it('a refused drop leaves the Space target where it was', async () => {
		entry.noteRecentDeck(2);
		await drop(4, { file_exists: false, is_streaming: false });
		assert.equal(entry.getRecentDeck(), 2);
	});
});

