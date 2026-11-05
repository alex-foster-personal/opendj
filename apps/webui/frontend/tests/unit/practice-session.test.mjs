// requirement: SKIN-8871D
// The /practice route starts a performance command session, so replica mixer moves land.
//
//   if a fader command is refused on /practice for want of a command session then broken
//   if uninstall leaves the command session active after the route unmounts then broken

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('practice session lets a mixer move dispatch, and uninstall ends it', async () => {
	const m = await loadTypeScriptModule('tests/unit/fixtures/practice-session-entry.ts');
	globalThis.window = {};
	try {
		// Negative control: no session, the dispatcher refuses.
		await assert.rejects(m.dispatchPerformanceCommand({ type: 'fader', deck: 1, value: 0.3 }));
		const uninstall = m.installPracticeSession();
		try {
			assert.notEqual(m.activePerformanceCommandSession(), null);
			await m.dispatchPerformanceCommand({ type: 'fader', deck: 1, value: 0.3 });
			assert.equal(m.queryPerformanceState().mixer.channels[1].fader, 0.3);
		} finally {
			uninstall();
		}
		assert.equal(m.activePerformanceCommandSession(), null);
	} finally {
		delete globalThis.window;
	}
});
