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
 * - if a hot-cue command is taken from the page or refused then broken: the
 *   page's own cue logic drives the engine through `rustHotCueDriver`
 * - if a late decode failure heard while a load is in flight is dropped then
 *   broken: the load publishes a track the engine already unloaded; if one
 *   that ended the earlier load is applied to the new one then broken too
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
	assert.ok(m.WEB_AUDIO_ONLY.has('safety_loop_save'));
	// Hot cues are the page's own logic, driving the engine through its driver.
	for (const t of ['hot_cue_save', 'hot_cue_trigger']) {
		assert.equal(m.ENGINE_COMMANDS.has(t) || m.WEB_AUDIO_ONLY.has(t), false, t);
	}
});

test('off: every command stays with the page', async () => {
	assert.equal(await m.executeInRustEngine({ type: 'play', deck: 1, playing: true }), false);
});

test('on: a Web Audio only command fails by name', async () => {
	m.rustMode.enabled = true;
	try {
		await assert.rejects(
			m.executeInRustEngine({ type: 'safety_loop_save', deck: 1 }),
			/RUST_ENGINE_UNAVAILABLE: safety_loop_save .* see PARITY-TODO/
		);
		// Not the engine's either: still the page's own.
		assert.equal(await m.executeInRustEngine({ type: 'browser_search', query: 'x' }), false);
		assert.equal(await m.executeInRustEngine({ type: 'hot_cue_clear', deck: 1, slot: 'A' }), false);
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

	st.cue_ms = 480;
	m.mirrorEngineState(frame(12, { position_ms: 250, cue_ms: 100 }));
	assert.equal(st.position_ms, 250, 'a newer frame is mirrored');
	assert.equal(st.cue_ms, 480, 'the memory cue stays the page one');
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

test('a late decode failure clears its deck and says why; an empty deck stays untouched', () => {
	const st = m.deckStates[2];
	st.stable_id = 'late';
	st.title = 'Late';
	st.processor_error = null;
	m.applyLoadFailed({
		type: 'load_failed',
		deck: 2,
		error: { code: 'decode', message: 'decode error in late.mp3: bad frame' }
	});
	assert.equal(st.stable_id, null, 'the page still shows a track the engine unloaded');
	assert.equal(st.title, null);
	assert.match(st.processor_error, /late\.mp3: bad frame/);

	// Control: a deck with nothing loaded is not given an error.
	const empty = m.deckStates[3];
	empty.stable_id = null;
	empty.processor_error = null;
	m.applyLoadFailed({ type: 'load_failed', deck: 3, error: { code: 'decode', message: 'x' } });
	assert.equal(empty.processor_error, null);
});

test('a late failure heard while a load is in flight is held until that load settles', () => {
	const st = m.deckStates[2];
	const fail = (path) => ({
		type: 'load_failed',
		deck: 2,
		path,
		error: { code: 'decode', message: `decode error in ${path}: bad frame` }
	});
	// The new load's own rest fails before the page has published it: the
	// failure is not dropped against the empty deck, it ends the load once
	// published.
	st.stable_id = null;
	st.processor_error = null;
	m.holdLoadFailures(2);
	m.applyLoadFailed(fail('/music/new.mp3'));
	assert.equal(st.processor_error, null, 'applied before the load it ends was shown');
	st.stable_id = 'new';
	m.releaseLoadFailures(2, '/music/new.mp3');
	assert.equal(st.stable_id, null, 'the published load outlived its failed rest');
	assert.match(st.processor_error, /new\.mp3: bad frame/);

	// Control: a failure that ended the earlier load on the deck is dropped,
	// never applied to the load that replaced it.
	st.stable_id = 'old';
	st.processor_error = null;
	m.holdLoadFailures(2);
	m.applyLoadFailed(fail('/music/old.mp3'));
	st.stable_id = 'new';
	m.releaseLoadFailures(2, '/music/new.mp3');
	assert.equal(st.stable_id, 'new', "an earlier load's failure cleared the new one");
	assert.equal(st.processor_error, null);

	// A load that never publishes leaves the earlier track shown: a failure
	// held meanwhile ends that one.
	st.stable_id = 'old';
	m.holdLoadFailures(2);
	m.applyLoadFailed(fail('/music/old.mp3'));
	m.releaseLoadFailures(2, null);
	assert.equal(st.stable_id, null, 'the earlier track stayed shown after the engine unloaded it');

	// Nothing stays held once a load settles.
	st.stable_id = 'next';
	st.processor_error = null;
	m.applyLoadFailed(fail('/music/next.mp3'));
	assert.equal(st.stable_id, null, 'a failure after the load settled was held');
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
