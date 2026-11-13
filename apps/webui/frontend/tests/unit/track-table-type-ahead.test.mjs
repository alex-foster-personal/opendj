import assert from 'node:assert/strict';
import { after, before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Type-ahead jump in the track grid (LIBM-82b, issue #12317) (Chromium select type-ahead, mimicked).
 *
 * [if] a typed title prefix does not select its first matching row [then] type-ahead jumps nowhere - broken.
 * [if] the same prefix again does not move to the next matching row [then] only the first match is reachable - broken.
 * [if] no matching row still moves the selection [then] a typo loses the DJ's place - broken.
 * [if] keys typed in a filter input reach type-ahead [then] typing a search also jumps the grid - broken.
 * [if] an open prefix lets deck shortcuts fire [then] typing "disco" cues a deck - broken.
 * [if] browser_type_ahead misses the table or takes a blank prefix [then] agents lose parity - broken.
 */

let ahead;
let ipc;

before(async () => {
	ahead = await loadTypeScriptModule('src/lib/components/rb/browser/track-table-type-ahead.ts');
	ipc = await loadTypeScriptModule('tests/unit/type-ahead-ipc-entry.mjs');
});

const TITLES = ['Acid Rain', 'Bassline', 'Blue Monday', 'Café del Mar', 'Blackwater', null];

function _row(attrs = {}) {
	const row = {
		tagName: 'TR',
		getAttribute: () => null,
		closest: (selector) => (selector.includes('track-row') ? row : null),
		...attrs
	};
	return row;
}

function _key(key, target, extra = {}) {
	return {
		key,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		defaultPrevented: false,
		timeStamp: 0,
		target,
		currentTarget: target,
		...extra
	};
}

describe('type-ahead matching', () => {
	it('a typed title prefix selects the first visible row starting with it', () => {
		/** [if] "bl" is typed with no active row [then] Blue Monday (index 2) is chosen, accents and case ignored, [else stop]. */
		assert.equal(ahead.typeAheadMatchIndex(TITLES, 'bl', -1), 2);
		assert.equal(ahead.typeAheadMatchIndex(TITLES, 'CAFE', -1), 3);
		assert.equal(ahead.typeAheadMatchIndex(TITLES, 'bl', 2), 2, 'typing on keeps the row');
	});

	it('the same letter again moves to the next matching row and wraps', () => {
		/** [if] "b" is typed on Bassline, then again on Blue Monday, then on Blackwater [then] it cycles 2, 4, 1, [else stop]. */
		assert.equal(ahead.typeAheadMatchIndex(TITLES, 'b', 1), 2);
		assert.equal(ahead.typeAheadMatchIndex(TITLES, 'b', 2), 4);
		assert.equal(ahead.typeAheadMatchIndex(TITLES, 'bb', 4), 1);
	});

	it('no matching row returns -1, so the selection stays', () => {
		/** [if] "zz" or an empty prefix is typed [then] no row is chosen, [else stop]. */
		assert.equal(ahead.typeAheadMatchIndex(TITLES, 'zz', 2), -1);
		assert.equal(ahead.typeAheadMatchIndex(TITLES, '', 2), -1);
		assert.equal(ahead.typeAheadMatchIndex([], 'a', -1), -1);
	});
});

describe('type-ahead keys', () => {
	it('a filter input or a key inside a row control never reaches type-ahead', () => {
		/** [if] "b" is typed in the search input or bubbles from a row button [then] charFor gives null, [else stop]. */
		const session = new ahead.TrackTypeAhead();
		const input = { tagName: 'INPUT', type: 'search', getAttribute: () => null };
		assert.equal(session.charFor(_key('b', input)), null);
		const row = _row();
		assert.equal(
			session.charFor({ ..._key('b', { tagName: 'BUTTON' }), currentTarget: row }),
			null
		);
		assert.equal(session.charFor(_key('b', row)), 'b');
	});

	it('shortcut keys never start a prefix, but an open prefix takes every printable key', () => {
		/** [if] W, space, a claimed C or Cmd+B would start a prefix [then] null; once "b" opens one, "s" joins it, [else stop]. */
		const session = new ahead.TrackTypeAhead();
		const row = _row();
		assert.equal(session.charFor(_key('w', row)), null);
		assert.equal(session.charFor(_key(' ', row)), null);
		assert.equal(session.charFor(_key('c', row, { defaultPrevented: true })), null);
		assert.equal(session.charFor(_key('b', row, { metaKey: true })), null);
		assert.equal(session.push('b', 100), 'b');
		assert.equal(session.charFor(_key('s', row, { timeStamp: 600 })), 's');
		assert.equal(session.push('l', 600), 'bl');
		assert.equal(session.push('u', 2000), 'u', 'a gap over the reset starts a new prefix');
	});

	it('an open prefix on a track row makes the global shortcuts yield', () => {
		/** [if] a prefix is open and "c" lands on a track row [then] ownsKey is true; closed or off-row it is false, [else stop]. */
		const session = new ahead.TrackTypeAhead();
		const row = _row();
		const offRow = { tagName: 'DIV', getAttribute: () => null, closest: () => null };
		assert.equal(session.ownsKey(_key('c', row, { timeStamp: 50 })), false);
		session.push('d', 0);
		assert.equal(session.ownsKey(_key('c', row, { timeStamp: 500 })), true);
		assert.equal(session.ownsKey(_key('c', offRow, { timeStamp: 500 })), false);
		assert.equal(session.ownsKey(_key('ArrowDown', row, { timeStamp: 500 })), false);
		assert.equal(session.ownsKey(_key('c', row, { timeStamp: 1500 })), false);
	});
});

describe('browser_type_ahead performance command', () => {
	let uninstall;
	before(() => {
		globalThis.window = {};
		uninstall = ipc.installPerformanceBrowserIpc();
	});
	after(() => {
		uninstall();
		delete globalThis.window;
	});

	it('jumps the mounted table and refuses a blank prefix, no match or no table', async () => {
		/** [if] an agent sends "blue" [then] the table jumps once; blank, unmatched and unmounted are refused, [else stop]. */
		const jumps = [];
		const unregister = ipc.trackTypeAhead.register({
			jump: (prefix) => {
				jumps.push(prefix);
				return prefix === 'blue' ? 'sid-blue' : null;
			}
		});
		try {
			await ipc.dispatchPerformanceCommand({ type: 'browser_type_ahead', prefix: 'blue' });
			assert.deepEqual(jumps, ['blue']);
			await assert.rejects(
				ipc.dispatchPerformanceCommand({ type: 'browser_type_ahead', prefix: '  ' }),
				/non-blank/
			);
			await assert.rejects(
				ipc.dispatchPerformanceCommand({ type: 'browser_type_ahead', prefix: 'zz' }),
				/no visible row starts with "zz"/
			);
		} finally {
			unregister();
		}
		await assert.rejects(
			ipc.dispatchPerformanceCommand({ type: 'browser_type_ahead', prefix: 'blue' }),
			/requires a mounted track table/
		);
	});
});
