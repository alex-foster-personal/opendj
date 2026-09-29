/**
 * Rust engine mode (NAE-13): the performance page driving odj-audio.
 *
 * Regression lines:
 * - if the default choice is anything but Web Audio then broken: the mode is
 *   opt-in until the switch-over plan (20-07)
 * - if an unknown ?engine= value silently falls back then broken
 * - if a command is both forwarded to the engine and refused as Web Audio only
 *   then broken: the two sets must not overlap
 * - if a Web Audio only command returns quietly in Rust mode then broken: it
 *   must fail by name, never look applied
 * - if a state frame from before a load lands on the newly loaded deck then
 *   broken: the old track's playhead would paint onto the new one
 * - if frames after the load are held back then broken (the overshoot): the
 *   deck would never show the engine's playhead
 * - if knob positions do not follow an acknowledged command then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/rust-mode-entry.ts');
});

function frame(n, deck) {
	return {
		type: 'state',
		frame: n,
		sample_rate: 48000,
		engine_time_ns: 0,
		decks: [
			{
				deck: 1,
				loaded: true,
				playing: false,
				position_ms: 0,
				duration_ms: 60000,
				rate: 0,
				tempo: 1,
				cue_ms: null,
				loop: null,
				...deck
			}
		],
		mixer: { crossfader: 0.5, master_volume: 1 },
		master: { muted: false }
	};
}

test('Web Audio stays the default; ?engine= chooses and outranks the stored choice', () => {
	assert.equal(m.readEngineChoice('', null), 'webaudio');
	assert.equal(m.readEngineChoice('?engine=rust', null), 'rust');
	assert.equal(m.readEngineChoice('', 'rust'), 'rust');
	assert.equal(m.readEngineChoice('?engine=webaudio', 'rust'), 'webaudio');
	assert.throws(() => m.readEngineChoice('?engine=native', null), /engine=native is not one of/);
	assert.throws(() => m.readEngineChoice('', 'bogus'), /engine=bogus is not one of/);
});

test('no command is both forwarded and refused', () => {
	const both = [...m.ENGINE_COMMANDS].filter((t) => m.WEB_AUDIO_ONLY.has(t));
	assert.deepEqual(both, []);
	// Positive control: the sets are populated, so an empty overlap means something.
	assert.ok(m.ENGINE_COMMANDS.has('play'));
	assert.ok(m.WEB_AUDIO_ONLY.has('hot_cue_save'));
});

test('off: every command stays with the page', async () => {
	assert.equal(await m.executeInRustEngine({ type: 'play', deck: 1, playing: true }), false);
});

test('on: a Web Audio only command fails by name', async () => {
	m.rustMode.enabled = true;
	try {
		await assert.rejects(
			m.executeInRustEngine({ type: 'hot_cue_save', deck: 1, slot: 'A' }),
			/RUST_ENGINE_UNAVAILABLE: hot_cue_save .* see PARITY-TODO/
		);
		// Not the engine's either: still the page's own.
		assert.equal(await m.executeInRustEngine({ type: 'browser_search', query: 'x' }), false);
	} finally {
		m.rustMode.enabled = false;
	}
});

test('the load fence holds back frames up to the one seen when the load landed', () => {
	const st = m.deckStates[1];
	st.stable_id = 'new-track';
	st.position_ms = 0;
	st.playing = false;

	m.loadFences[1] = Infinity;
	m.mirrorEngineState(frame(10, { position_ms: 14813 }));
	assert.equal(st.position_ms, 0, 'in flight: nothing is mirrored');

	m.loadFences[1] = 11;
	m.mirrorEngineState(frame(11, { position_ms: 14813 }));
	assert.equal(st.position_ms, 0, 'the frame current at landing may predate the swap');

	m.mirrorEngineState(frame(12, { position_ms: 250, cue_ms: 100 }));
	assert.equal(st.position_ms, 250, 'a newer frame is mirrored');
	assert.equal(st.cue_ms, 100);
	assert.equal(st.duration_ms, 60000);

	m.mirrorEngineState(frame(13, { playing: true, rate: 1, position_ms: 300, tempo: 1.02 }));
	assert.equal(st.playing, true);
	assert.equal(st.pitch, 1.02);
	delete m.loadFences[1];
});

test('an empty deck is never written from the feed', () => {
	const st = m.deckStates[1];
	st.stable_id = null;
	st.position_ms = 0;
	st.playing = false;
	m.mirrorEngineState(frame(100, { position_ms: 9000, playing: true }));
	assert.equal(st.position_ms, 0);
	assert.equal(st.playing, false);
});

test('knobs follow acknowledged commands; unload clears the deck', () => {
	m.applyAcknowledged({ type: 'crossfader', value: 0.2 });
	assert.equal(m.mixerState.crossfader, 0.2);
	m.applyAcknowledged({ type: 'eq', deck: 2, band: 'low', value: 0.1 });
	assert.equal(m.mixerState.channels[2].eq_low, 0.1);
	m.applyAcknowledged({ type: 'pitch_range', deck: 3, range: 8 });
	assert.equal(m.pitchRanges[3], 8);

	const st = m.deckStates[4];
	st.stable_id = 'x';
	st.title = 'T';
	st.position_ms = 5;
	m.applyAcknowledged({ type: 'unload', deck: 4 });
	assert.equal(st.stable_id, null);
	assert.equal(st.title, null);
	assert.equal(st.position_ms, 0);
});

test('Settings > Audio engine chooses the next load, Web Audio by default', async () => {
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts');
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const store = new Map();
	const prior = globalThis.window;
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, String(v))
		}
	};
	try {
		assert.ok(apply.ALLOWED_SETTING_KEYS.includes('audio_engine'));
		const def = catalog.SETTINGS_CATALOG.find((d) => d.id === 'audio_engine');
		assert.ok(def?.implemented, 'the row renders live');
		assert.deepEqual(
			def.control.options.map((o) => o.value),
			['webaudio', 'rust']
		);

		assert.equal(apply.readSettingValue('audio_engine'), 'webaudio');
		apply.applySettingChange('audio_engine', 'rust');
		assert.equal(store.get(m.ENGINE_PREF_KEY), 'rust');
		assert.equal(apply.readSettingValue('audio_engine'), 'rust');
		apply.applySettingChange('audio_engine', 'webaudio');
		assert.equal(apply.readSettingValue('audio_engine'), 'webaudio');

		assert.throws(() => apply.applySettingChange('audio_engine', 'native'), /webaudio\|rust/);
		assert.equal(store.get(m.ENGINE_PREF_KEY), 'webaudio', 'a refused value writes nothing');
		// A corrupted stored value is loud, never quietly Web Audio.
		store.set(m.ENGINE_PREF_KEY, 'bogus');
		assert.throws(() => apply.readSettingValue('audio_engine'), /engine=bogus/);
	} finally {
		globalThis.window = prior;
	}
});
