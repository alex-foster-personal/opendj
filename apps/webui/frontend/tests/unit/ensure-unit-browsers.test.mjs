/**
 * #12781: the unit gate provisions Chromium under CI and only under CI.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ensureUnitBrowsers, isCi } from '../../scripts/ensure-unit-browsers.mjs';

/** Build fake seams that record every install command. */
function fakes({ ci, present, status = 0 }) {
	const calls = [];
	return {
		calls,
		opts: {
			env: ci === undefined ? {} : { CI: ci },
			executablePath: () => '/Users/dev/ms-playwright/chromium/chrome',
			exists: () => present,
			run: (cmd, args) => {
				calls.push([cmd, ...args]);
				return status;
			}
		}
	};
}

test('isCi reads common CI values', () => {
	assert.equal(isCi({ CI: 'true' }), true);
	assert.equal(isCi({ CI: '1' }), true);
	assert.equal(isCi({ CI: 'false' }), false);
	assert.equal(isCi({ CI: '0' }), false);
	assert.equal(isCi({}), false);
});

test('installs Chromium under CI when the binary is missing', () => {
	const { calls, opts } = fakes({ ci: 'true', present: false });
	assert.equal(ensureUnitBrowsers(opts), 0);
	assert.deepEqual(calls, [['pnpm', 'exec', 'playwright', 'install', 'chromium']]);
});

test('propagates a failed install so the gate fails loudly', () => {
	const { opts } = fakes({ ci: 'true', present: false, status: 1 });
	assert.equal(ensureUnitBrowsers(opts), 1);
});

test('does nothing under CI when Chromium is already present', () => {
	const { calls, opts } = fakes({ ci: 'true', present: true });
	assert.equal(ensureUnitBrowsers(opts), 0);
	assert.deepEqual(calls, []);
});

test('does nothing locally, so a missing browser still fails the test', () => {
	const { calls, opts } = fakes({ present: false });
	assert.equal(ensureUnitBrowsers(opts), 0);
	assert.deepEqual(calls, []);
});
