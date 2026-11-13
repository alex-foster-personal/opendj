import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * Issue #12209: the TopBar master mute is a safety control sent through the
 * performance dispatcher, and it renders `isMasterMuted()`. In Rust engine mode
 * the dispatcher forwards `master_mute` to odj-audio, which can refuse it (an
 * `ok: false` result, or a closed socket). A refused mute must not flip the
 * shown state or leave the dispatcher pending, the refusal must be exposed, and
 * a later mute the engine accepts must show on the TopBar.
 */

globalThis.location ??= { search: '' };

let m;

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/master-mute-rust-entry.ts');
});

test('a master mute the Rust engine refuses keeps the acknowledged state, clears pending, exposes the error, and a retry lands', async () => {
	// [if] the engine refuses master mute [then] state, pending flag and error stay truthful, [else stop].
	globalThis.window = {};
	const uninstall = m.installPerformanceBrowserIpc();
	const sent = [];
	let refuse = true;
	m.installRustEngineClientForTest({
		connected: true,
		notBuilt: new Set(),
		send: (cmd) => {
			sent.push(cmd);
			if (refuse) return Promise.reject(new m.EngineCommandError('invalid', 'master mute refused'));
			return Promise.resolve({ type: 'result', id: null, ok: true });
		},
		positionMs: () => null
	});
	// Settle the mode's one-time init first, or it would reset the flag below.
	m.initRustMode();
	m.rustMode.enabled = true;
	try {
		const acknowledged = m.isMasterMuted();
		assert.equal(m.queryPerformanceState().master.muted, acknowledged);

		// persist:false keeps this test off localStorage and the ui-prefs PUT.
		const command = { type: 'master_mute', muted: !acknowledged, persist: false };
		const refused = await m.runPerformanceCommandFromUi(command);
		assert.equal(refused.ok, false, 'a refused master mute must not report success');
		assert.deepEqual(
			sent.map((c) => c.type),
			['master_mute'],
			'the mute must reach the engine'
		);

		const afterRefusal = m.queryPerformanceState();
		assert.equal(m.isMasterMuted(), acknowledged, 'refusal must not flip the TopBar mute');
		assert.equal(afterRefusal.master.muted, acknowledged);
		assert.equal(afterRefusal.command_pending, false, 'refusal must not leave the control pending');
		assert.equal(afterRefusal.command_queued, 0);
		assert.match(
			afterRefusal.last_error ?? '',
			/master mute refused/,
			'refusal must expose its error'
		);

		refuse = false;
		const retried = await m.runPerformanceCommandFromUi(command);
		assert.deepEqual(retried, { ok: true });
		assert.equal(m.isMasterMuted(), !acknowledged, 'the TopBar must show the acknowledged mute');
		assert.equal(m.queryPerformanceState().master.muted, !acknowledged);
		assert.equal(m.queryPerformanceState().command_pending, false);
	} finally {
		refuse = false;
		await m.runPerformanceCommandFromUi({ type: 'master_mute', muted: false, persist: false });
		m.installRustEngineClientForTest(null);
		m.rustMode.enabled = false;
		uninstall();
		delete globalThis.window;
	}
});
