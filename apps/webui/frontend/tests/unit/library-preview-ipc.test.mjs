// requirement: LIBWS-04
// [if] an agent calls window.musicDjToolsLibraryPreview [then] play, pause, seek and stop take the preview bar's player and return its state
// [if] the agent's track is a selected row that cannot play [then] play refuses with the bar's reason and the player is not called
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipcModule;

before(async () => {
	ipcModule = await loadTypeScriptModule('src/lib/library/library-preview-ipc.ts');
});

function fakePlayer() {
	const calls = [];
	const state = {
		stable_id: null,
		status: 'idle',
		position_ms: 0,
		duration_ms: 0,
		error: null
	};
	return {
		calls,
		state,
		async play(id, ratio) {
			calls.push(['play', id, ratio]);
			Object.assign(state, {
				stable_id: id,
				status: 'playing',
				duration_ms: 1000,
				position_ms: (ratio ?? 0) * 1000
			});
		},
		pause() {
			calls.push(['pause']);
			if (state.status === 'playing') state.status = 'paused';
		},
		stop() {
			calls.push(['stop']);
			Object.assign(state, {
				stable_id: null,
				status: 'idle',
				position_ms: 0,
				duration_ms: 0
			});
		},
		snapshot: () => state
	};
}

test('play, pause, seek and stop drive the same player and read back its state', async () => {
	const player = fakePlayer();
	const ipc = ipcModule.createLibraryPreviewIpc({
		player,
		refusal: () => null
	});
	assert.equal(ipc.version, 1);
	assert.equal(ipc.query().status, 'idle');

	let s = await ipc.play('t1');
	assert.deepEqual(player.calls.at(-1), ['play', 't1', undefined]);
	assert.equal(s.status, 'playing');
	assert.equal(s.stable_id, 't1');

	s = ipc.pause();
	assert.equal(s.status, 'paused');

	s = await ipc.seek(0.5);
	assert.deepEqual(player.calls.at(-1), ['play', 't1', 0.5]);
	assert.equal(s.position_ms, 500);

	s = ipc.stop();
	assert.deepEqual(player.calls.at(-1), ['stop']);
	assert.equal(s.stable_id, null);
	assert.ok(Object.isFrozen(s), 'query state is a snapshot, not the live store');
});

test('a refused track never reaches the player', async () => {
	const player = fakePlayer();
	const ipc = ipcModule.createLibraryPreviewIpc({
		player,
		refusal: (id) => (id === 'gone' ? 'file is missing' : null)
	});
	await assert.rejects(ipc.play('gone'), /preview: file is missing/);
	assert.deepEqual(player.calls, []);
	await ipc.play('ok', 0.25);
	assert.deepEqual(player.calls, [['play', 'ok', 0.25]]);
});

test('bad arguments fail loudly instead of guessing', async () => {
	const player = fakePlayer();
	const ipc = ipcModule.createLibraryPreviewIpc({
		player,
		refusal: () => null
	});
	await assert.rejects(ipc.play(''), /non-empty stable_id/);
	await assert.rejects(ipc.play('t1', 2), /ratio between 0 and 1/);
	await assert.rejects(ipc.seek(0.5), /no preview is loaded/);
	await ipc.play('t1');
	await assert.rejects(ipc.seek(Number.NaN), /ratio between 0 and 1/);
	assert.deepEqual(player.calls, [['play', 't1', undefined]]);
});

test('install fails fast outside a browser window', () => {
	const ipc = ipcModule.createLibraryPreviewIpc({
		player: fakePlayer(),
		refusal: () => null
	});
	assert.throws(() => ipcModule.installLibraryPreviewIpc(ipc), /requires a browser window/);
});
