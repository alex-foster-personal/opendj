/**
 * Issue #12947: Cmd K focus return helpers.
 *
 * // requirement: CMDK-14411
 * // [if] the bar closes with focus still inside it [then] the captured opener is focused again, else stop
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
let realDocument;

function fakeEl(extra = {}) {
	return {
		tabIndex: 0,
		disabled: false,
		isConnected: true,
		matches: () => false,
		closest: () => null,
		getClientRects: () => [{ width: 1, height: 1 }],
		focus() {},
		blur() {},
		...extra
	};
}

test.before(async () => {
	mod = await loadTypeScriptModule('src/lib/cmdk/command-bar-focus.ts');
});

test.afterEach(() => {
	if (realDocument !== undefined) globalThis.document = realDocument;
});

test('captureCommandBarOpener keeps focusable controls outside the bar', () => {
	realDocument = globalThis.document;
	const body = fakeEl({ label: 'body' });
	const opener = fakeEl({ label: 'opener' });
	const bar = { contains: () => false };
	globalThis.document = { body };

	assert.equal(mod.captureCommandBarOpener(opener, bar), opener);
	assert.equal(mod.captureCommandBarOpener(body, bar), null);
	assert.equal(mod.captureCommandBarOpener(null, bar), null);
	assert.equal(mod.captureCommandBarOpener(opener, { contains: () => true }), null);
});

test('restoreCommandBarFocus returns to the opener after dismiss', () => {
	realDocument = globalThis.document;
	const calls = [];
	const opener = fakeEl({
		label: 'opener',
		focus() {
			calls.push('opener');
		}
	});
	const trapped = fakeEl({ label: 'input' });
	const bar = {
		contains(el) {
			return el === trapped;
		}
	};
	globalThis.document = { activeElement: trapped, body: fakeEl() };

	mod.restoreCommandBarFocus(opener, bar);
	assert.deepEqual(calls, ['opener']);
});
