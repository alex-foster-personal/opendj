// requirement: DECKGAP-7691
// Review r4404895946: the reset choice is a performance control, so the panel
// and agents take ONE path: the typed `track_load_reset` command on the shared
// dispatcher, with the current choice in query state (AGENTS.md parity rule).
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc, app, reactive;
before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
	app = await loadTypeScriptModule('tests/unit/fixtures/pairing-snapshot-entry.ts', {
		viteApiBase: 'https://pairing.example.test'
	});
	reactive = await loadTypeScriptModule('src/lib/rb/track-load-reset.svelte.ts');
});

const ALL = { level: true, stems: false, eq: true, trim: false, filter: true };

test('the command parses only all five booleans and has no deck queue scope', () => {
	assert.deepEqual(ipc.parsePerformanceCommandForTest({ type: 'track_load_reset', ...ALL }), {
		type: 'track_load_reset',
		...ALL
	});
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'track_load_reset', ...ALL }), null);
	// Controls: a missing item, a non-boolean, or an extra key is refused, not guessed.
	const { filter: _omit, ...four } = ALL;
	assert.throws(() => ipc.parsePerformanceCommandForTest({ type: 'track_load_reset', ...four }));
	assert.throws(() =>
		ipc.parsePerformanceCommandForTest({ type: 'track_load_reset', ...ALL, eq: 'yes' })
	);
	assert.throws(() =>
		ipc.parsePerformanceCommandForTest({ type: 'track_load_reset', ...ALL, deck: 1 })
	);
});

test('dispatching the command changes the choice and query state shows it', async () => {
	const hadWindow = 'window' in globalThis;
	if (!hadWindow) globalThis.window = {};
	const uninstall = app.installPerformanceBrowserIpc();
	try {
		await check();
	} finally {
		uninstall();
		if (!hadWindow) delete globalThis.window;
	}
});

async function check() {
	await app.dispatchPerformanceCommand({ type: 'track_load_reset', ...ALL });
	assert.deepEqual(app.queryPerformanceState().ui.track_load_reset, ALL);
	const flipped = { level: false, stems: true, eq: false, trim: true, filter: false };
	await app.dispatchPerformanceCommand({ type: 'track_load_reset', ...flipped });
	assert.deepEqual(app.queryPerformanceState().ui.track_load_reset, flipped);
	// The snapshot is a copy: an agent mutating it cannot change the stored choice.
	app.queryPerformanceState().ui.track_load_reset.level = true;
	assert.equal(app.queryPerformanceState().ui.track_load_reset.level, false);
}

test('the panel changes one item by dispatching the full command, never by writing the store', async () => {
	const cmd = reactive.trackLoadResetCommand('eq', true);
	assert.equal(cmd.type, 'track_load_reset');
	assert.equal(cmd.eq, true);
	assert.deepEqual(Object.keys(cmd).sort(), ['eq', 'filter', 'level', 'stems', 'trim', 'type']);
	const panel = await readFile('src/lib/components/rb/TrackLoadResetPanel.svelte', 'utf8');
	assert.match(
		panel,
		/runPerformanceCommandFromUi\(trackLoadResetCommand\(item\.id, event\.currentTarget\.checked\)\)/
	);
	assert.doesNotMatch(panel, /writeTrackLoadReset|setTrackLoadReset/);
});
