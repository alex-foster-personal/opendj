import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://audio-engine.example.test';

let audio;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: API_BASE
	});
});

test('pause on a never-loaded deck is a no-op and does not throw', async () => {
	/** [if] pause on an unloaded deck [then] no-op and no throw, [else stop]. */
	const st = audio.getDeckState(1);
	assert.equal(st.stable_id, null);
	await audio.engine.pause(1);
	assert.equal(st.playing, false);
	assert.equal(st.audible, false);
	assert.equal(st.transport_pending, false);
	assert.equal(st.stable_id, null);
});

test('pause clears stale live flags on an unloaded deck', async () => {
	/** [if] pause with stale live flags [then] flags clear, [else stop]. */
	const st = audio.deckStates[1];
	st.playing = true;
	st.audible = true;
	st.transport_pending = true;
	await audio.engine.pause(1);
	assert.equal(st.playing, false);
	assert.equal(st.audible, false);
	assert.equal(st.transport_pending, false);
	assert.equal(st.stable_id, null);
});

test('play on an unloaded deck still refuses', async () => {
	/** [if] play on an unloaded deck [then] throw no track loaded, [else stop]. */
	await assert.rejects(audio.engine.play(1), /play: no track loaded on deck 1/);
});
