/**
 * Virtual-edit fields on deck load (LIBEDIT, issue #7881).
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let deckLoadEditWindow;

before(async () => {
	({ deckLoadEditWindow } = await loadTypeScriptModule('src/lib/rb/deck-load-edit-window.ts'));
});

describe('deckLoadEditWindow', () => {
	it('a full edit row gives edit_id, in_ms and out_ms', () => {
		assert.deepEqual(deckLoadEditWindow({ edit_id: 'edit-1', in_ms: 1000, out_ms: 5000 }), {
			edit_id: 'edit-1',
			in_ms: 1000,
			out_ms: 5000
		});
	});

	it('a plain row with null edit window fields gives an empty object', () => {
		assert.deepEqual(deckLoadEditWindow({ edit_id: null, in_ms: null, out_ms: null }), {});
	});

	it('in_ms 0 is kept because it is a real in point', () => {
		assert.deepEqual(deckLoadEditWindow({ in_ms: 0, out_ms: 100 }), { in_ms: 0, out_ms: 100 });
	});

	it('empty edit_id is dropped', () => {
		assert.deepEqual(deckLoadEditWindow({ edit_id: '', in_ms: 1, out_ms: 2 }), {
			in_ms: 1,
			out_ms: 2
		});
	});
});
