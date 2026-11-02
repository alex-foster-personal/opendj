// requirement: DECKGAP-7691
// Review r4405564832: with ?engine=rust the reset choice did nothing, because
// only RbAudioEngine.load applied it. The Rust load path now sends the chosen
// resets as ordinary, acknowledged mixer commands.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m, sent, refuse;
before(async () => { m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts'); });
beforeEach(() => {
	sent = []; refuse = null;
	m.installRustEngineClientForTest({
		connected: true, notBuilt: new Set(),
		send: (cmd) => { sent.push(cmd); return refuse === cmd.type ? Promise.reject(new Error('refused')) : Promise.resolve({ type: 'result', id: null, ok: true }); },
		positionMs: () => null
	});
	m.rustMode.enabled = true;
	Object.assign(m.deckStates[2], { stable_id: 'track-2', load_generation: 5 });
	Object.assign(m.mixerState.channels[2], { fader: 0, trim: 0.9, eq_high: 0.5, eq_mid: 0.2, eq_low: 0.5, filter: 0.7 });
});
after(() => { m.installRustEngineClientForTest(null); m.rustMode.enabled = false; });

test('the chosen resets go to the engine and land in mixer state once acknowledged', async () => {
	m.writeTrackLoadReset({ level: true, stems: true, eq: true, trim: false, filter: true });
	await m.resetRustChannelOnLoad(2, 5, 'track-2');
	assert.deepEqual(sent.map((c) => [c.type, c.band ?? null, c.value]), [['fader', null, 1], ['eq', 'mid', 0.5], ['filter', null, 0.5]]);
	assert.equal(m.mixerState.channels[2].fader, 1);
	assert.equal(m.mixerState.channels[2].eq_mid, 0.5);
	assert.equal(m.mixerState.channels[2].trim, 0.9, 'trim is off, so it is left alone');
});

test('control: a superseded load sends nothing, and a refused command is not published', async () => {
	m.writeTrackLoadReset({ level: true, stems: true, eq: false, trim: false, filter: false });
	await m.resetRustChannelOnLoad(2, 4, 'track-2');
	assert.deepEqual(sent, []);
	refuse = 'fader';
	await assert.rejects(m.resetRustChannelOnLoad(2, 5, 'track-2'), /refused/);
	assert.equal(m.mixerState.channels[2].fader, 0, 'a refused reset is never shown as applied');
});

test('the Rust load path runs the reset for the load it started', async () => {
	const src = await readFile('src/lib/audio-engine/rust-engine.ts', 'utf8');
	const load = src.slice(src.indexOf('export async function loadRustDeck('), src.indexOf('export async function resetRustChannelOnLoad('));
	assert.match(load, /const generation = deckStates\[deck\]\.load_generation;/);
	assert.match(load, /await resetRustChannelOnLoad\(deck, generation, stable_id\);/);
});
