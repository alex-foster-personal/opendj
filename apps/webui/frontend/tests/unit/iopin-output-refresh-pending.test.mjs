import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	refusalMessageFromUiResult,
	runOutputRefresh
} from '../../src/lib/rb/output-refresh-ui.ts';

function uiState() {
	let pending = false;
	let localRefusal = null;
	return {
		get pending() {
			return pending;
		},
		get localRefusal() {
			return localRefusal;
		},
		hooks: {
			setPending(value) {
				pending = value;
			},
			setLocalRefusal(value) {
				localRefusal = value;
			}
		}
	};
}

test('if refresh rejects then pending clears and the named error is exposed', async () => {
	const panel = uiState();
	const refusal = 'no command session';
	await runOutputRefresh(
		async () => ({ ok: false, reason: 'no_session', error: new Error(refusal) }),
		panel.hooks
	);
	assert.equal(panel.pending, false);
	assert.equal(panel.localRefusal, refusal);
	assert.equal(
		refusalMessageFromUiResult({ ok: false, reason: 'no_session', error: new Error(refusal) }),
		refusal
	);
});

test('if the next refresh succeeds then the local refusal line is cleared', async () => {
	const panel = uiState();
	await runOutputRefresh(
		async () => ({ ok: false, reason: 'no_session', error: new Error('stale session') }),
		panel.hooks
	);
	assert.equal(panel.localRefusal, 'stale session');
	await runOutputRefresh(async () => ({ ok: true }), panel.hooks);
	assert.equal(panel.pending, false);
	assert.equal(panel.localRefusal, null);
});

test('if the engine already published device_access then no duplicate local refusal is kept', async () => {
	const panel = uiState();
	await runOutputRefresh(
		async () => ({
			ok: false,
			reason: 'failed',
			error: new Error('enumeration failed: device hub offline')
		}),
		panel.hooks
	);
	assert.equal(panel.pending, false);
	assert.equal(panel.localRefusal, null);
});
