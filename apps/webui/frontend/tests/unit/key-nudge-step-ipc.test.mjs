// requirement: DECKGAP-7691b
// Review r4405564818: the key nudge step is a performance control, so the
// deck header's step buttons and agents take ONE path: the typed
// `key_nudge_step` command, with the current step in query state.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc, app;
before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	app = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
		viteApiBase: 'https://pairing.example.test'
	});
});

test('the command parses one known step and has no deck queue scope', () => {
	for (const step of ['semitone', 'tone', 'scale', 'camelot']) {
		assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'key_nudge_step', step }), {
			type: 'key_nudge_step',
			step
		});
	}
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'key_nudge_step', step: 'tone' }), null);
	// Controls: an unknown step, a missing one, or an extra key is refused.
	assert.throws(
		() => ipc.parsePerformanceCommandForTest({ type: 'key_nudge_step', step: 'octave' }),
		/key_nudge_step step must be one of/
	);
	assert.throws(
		() => ipc.parsePerformanceCommandForTest({ type: 'key_nudge_step' }),
		/key_nudge_step step must be one of/
	);
	assert.throws(() =>
		ipc.parsePerformanceCommandForTest({ type: 'key_nudge_step', step: 'tone', deck: 1 })
	);
});

test('dispatching the command changes the step and query state shows it', async () => {
	const hadWindow = 'window' in globalThis;
	if (!hadWindow) globalThis.window = {};
	const uninstall = app.installPerformanceBrowserIpc();
	try {
		assert.equal(
			app.queryPerformanceState().ui.key_nudge_step,
			'semitone',
			'semitone is the default'
		);
		await app.dispatchPerformanceCommand({ type: 'key_nudge_step', step: 'camelot' });
		assert.equal(app.queryPerformanceState().ui.key_nudge_step, 'camelot');
		assert.equal(app.readKeyNudgeStep(), 'camelot');
		await app.dispatchPerformanceCommand({ type: 'key_nudge_step', step: 'tone' });
		assert.equal(app.queryPerformanceState().ui.key_nudge_step, 'tone');
	} finally {
		await app.dispatchPerformanceCommand({ type: 'key_nudge_step', step: 'semitone' });
		uninstall();
		if (!hadWindow) delete globalThis.window;
	}
});

test('the header step buttons dispatch the command and read the shared step, never set it locally', async () => {
	const header = await readFile('src/lib/components/rb/deck/DeckHeader.svelte', 'utf8');
	assert.match(
		header,
		/onclick=\{\(\) => void runPerformanceCommandFromUi\(\{ type: 'key_nudge_step', step: step\.id \}\)\}/
	);
	assert.doesNotMatch(header, /onclick=\{\(\) => \(keyNudgeStep = /);
	assert.match(header, /subscribeKeyNudgeStep\(\(step\) => \{\s*keyNudgeStep = step;/);
});
