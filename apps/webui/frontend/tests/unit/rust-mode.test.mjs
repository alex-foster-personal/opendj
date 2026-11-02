/**
 * Rust engine mode (NAE-13): the performance page driving odj-audio.
 *
 * Regression lines:
 * - if the default choice is anything but Web Audio in a browser tab or Tauri
 *   then broken; if it is anything but Rust inside the Electron shell then
 *   broken (Fri 9 Oct 2026); an explicit or stored choice outranks either
 * - if a choice made inside Electron is kept only in localStorage then broken:
 *   the engine's port, so the page's origin, changes each launch (Codex
 *   4236763903); the shell's saved choice must outrank it
 * - if the library's load-and-play intent is refused in Rust mode then broken:
 *   every double-click or "load and play" would silently load nothing
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
import { readFileSync } from 'node:fs';
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

test('Rust is the default inside the Electron shell only; a choice outranks it', () => {
	const electron = { opendjShell: { kind: 'electron', version: 1 } };
	assert.equal(m.defaultEngineChoice(electron), 'rust');
	assert.equal(m.defaultEngineChoice({}), 'webaudio');
	assert.equal(m.defaultEngineChoice({ __TAURI_INTERNALS__: {} }), 'webaudio');
	// A bridge that is not the Electron one is not Electron.
	assert.equal(m.defaultEngineChoice({ opendjShell: { kind: 'other' } }), 'webaudio');
	assert.equal(m.readEngineChoice('', null, 'rust'), 'rust');
	assert.equal(m.readEngineChoice('?engine=webaudio', null, 'rust'), 'webaudio');
	assert.equal(m.readEngineChoice('', 'webaudio', 'rust'), 'webaudio');
	assert.equal(m.readEngineChoice('', null, 'webaudio'), 'webaudio');
});

test('inside Electron the shell-saved choice outranks this origin\'s storage, and a new choice is saved to the shell', async () => {
	const store = new Map([[m.ENGINE_PREF_KEY, 'rust']]);
	const saved = [];
	const prior = { window: globalThis.window, shell: globalThis.opendjShell, choice: globalThis.OPENDJ_AUDIO_ENGINE_CHOICE };
	globalThis.window = {
		localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) }
	};
	globalThis.opendjShell = { kind: 'electron', version: 1, setAudioEngineChoice: async (c) => void saved.push(c) };
	try {
		// Nothing saved by the shell: this origin's storage still decides.
		delete globalThis.OPENDJ_AUDIO_ENGINE_CHOICE;
		assert.equal(m.storedEngineChoice(), 'rust');
		// The shell's saved choice is the one that survives a relaunch's new origin.
		globalThis.OPENDJ_AUDIO_ENGINE_CHOICE = 'webaudio';
		assert.equal(m.storedEngineChoice(), 'webaudio');
		m.setStoredEngineChoice('rust');
		assert.deepEqual(saved, ['rust']);
		// Published only once the shell acknowledges the save.
		assert.equal(globalThis.OPENDJ_AUDIO_ENGINE_CHOICE, 'webaudio');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.equal(globalThis.OPENDJ_AUDIO_ENGINE_CHOICE, 'rust');
		assert.equal(m.rustMode.saved, 'rust');
		assert.equal(m.storedEngineChoice(), 'rust');
		// A save the shell refuses (disk full) is reported, and the previous
		// choice stays the one this page and the next launch see (Codex 4430422537).
		const reported = [];
		const priorError = console.error;
		console.error = (...args) => void reported.push(args.map(String).join(' '));
		globalThis.opendjShell.setAudioEngineChoice = async () => {
			throw new Error('ENOSPC');
		};
		try {
			m.setStoredEngineChoice('webaudio');
			await new Promise((resolve) => setTimeout(resolve, 0));
		} finally {
			console.error = priorError;
		}
		assert.equal(globalThis.OPENDJ_AUDIO_ENGINE_CHOICE, 'rust');
		assert.equal(m.rustMode.saved, 'rust');
		assert.equal(m.storedEngineChoice(), 'rust');
		assert.equal(reported.length, 1);
		assert.match(reported[0], /Audio engine choice not saved.*ENOSPC/);
		// Outside Electron nothing is sent to a shell.
		delete globalThis.opendjShell;
		m.setStoredEngineChoice('webaudio');
		assert.deepEqual(saved, ['rust']);
		assert.equal(store.get(m.ENGINE_PREF_KEY), 'webaudio');
	} finally {
		globalThis.window = prior.window;
		if (prior.shell === undefined) delete globalThis.opendjShell;
		else globalThis.opendjShell = prior.shell;
		if (prior.choice === undefined) delete globalThis.OPENDJ_AUDIO_ENGINE_CHOICE;
		else globalThis.OPENDJ_AUDIO_ENGINE_CHOICE = prior.choice;
	}
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

test('page-level preference toggles are not refused in rust mode (Codex 4286901139)', () => {
	// Two-track AutoPlay, the perf view and the PAD toggle only write a ui pref
	// the page reads; none touches the Web Audio clock, buffers or devices.
	for (const t of ['auto_play_two_track', 'set_perf_view', 'show_pads']) {
		assert.equal(m.WEB_AUDIO_ONLY.has(t), false, t);
	}
	// Control: a command that does reach into Web Audio is still refused.
	assert.ok(m.WEB_AUDIO_ONLY.has('auto_play_next_arm'));
});

test('pairing capture is not refused in rust mode (#7146, pairings in the Electron shell)', () => {
	// Open, EQ-adjuster removal and Capture read the frozen snapshot, live tempo
	// and master, and write over the pairings HTTP routes; none touches the Web
	// Audio clock, buffers or devices. Refusing Capture alone left the Create
	// pairing sheet unable to save under the Rust engine.
	for (const t of ['pairing_snapshot_open', 'pairing_snapshot_remove_eq_adjuster', 'pairing_snapshot_save']) {
		assert.equal(m.WEB_AUDIO_ONLY.has(t), false, t);
		assert.equal(m.ENGINE_COMMANDS.has(t), false, t);
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

test('on: the load-and-play intent stays the page\'s own, never refused', async () => {
	// The library sends load_play_intent before every load; refusing it threw
	// before the load was ever sent, so nothing loaded in Rust mode.
	assert.equal(m.WEB_AUDIO_ONLY.has('load_play_intent'), false);
	assert.equal(m.ENGINE_COMMANDS.has('load_play_intent'), false);
	m.rustMode.enabled = true;
	try {
		assert.equal(
			await m.executeInRustEngine({ type: 'load_play_intent', deck: 1, generation: 1, desired_play: true }),
			false
		);
		// Control: a real Web Audio only command still refuses in the same state.
		await assert.rejects(m.executeInRustEngine({ type: 'safety_loop_arm', deck: 1 }), /RUST_ENGINE_UNAVAILABLE/);
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

test('restart to load engine only after this page has committed to a different engine', () => {
	const store = new Map();
	const prior = globalThis.window;
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, String(v))
		},
		location: { search: '' }
	};
	const priorEnabled = m.rustMode.enabled;
	const priorSaved = m.rustMode.saved;
	const priorCommitted = m.rustMode.committed;
	try {
		m.rustMode.enabled = false;
		m.rustMode.saved = null;
		m.rustMode.committed = false;
		m.setStoredEngineChoice('rust');
		assert.equal(
			m.engineRestartNeeded(),
			false,
			'before the page commits, the next load picks the engine up straight away'
		);
		m.rustMode.committed = true;
		m.rustMode.enabled = false;
		m.rustMode.saved = 'webaudio';
		m.setStoredEngineChoice('rust');
		assert.equal(m.engineRestartNeeded(), true);
		m.setStoredEngineChoice('webaudio');
		assert.equal(m.engineRestartNeeded(), false, 'changing back to the live engine clears it');
	} finally {
		m.rustMode.enabled = priorEnabled;
		m.rustMode.saved = priorSaved;
		m.rustMode.committed = priorCommitted;
		globalThis.window = prior;
	}
});

test('restart to load engine is one command: the Settings button and the agent bridge run it alike', () => {
	// Sol P1, PR #6829: the restart was a click handler only, with no agent path.
	assert.deepEqual(m.engineRestartTarget('http://h/performance?engine=rust&x=1'), {
		kind: 'assign',
		href: 'http://h/performance?x=1'
	});
	assert.deepEqual(m.engineRestartTarget('http://h/performance?x=1'), { kind: 'reload' });
	const prior = globalThis.window;
	const priorState = { enabled: m.rustMode.enabled, saved: m.rustMode.saved, committed: m.rustMode.committed };
	const calls = [];
	const store = new Map();
	globalThis.window = {
		localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) },
		location: {
			href: 'http://h/performance?engine=webaudio',
			search: '?engine=webaudio',
			assign: (href) => calls.push(['assign', href]),
			reload: () => calls.push(['reload'])
		}
	};
	try {
		m.rustMode.committed = true;
		m.rustMode.enabled = false;
		m.rustMode.saved = 'webaudio';
		m.setStoredEngineChoice('webaudio');
		assert.throws(() => m.restartToLoadEngine(), /already the one this page runs/, 'a restart with nothing to load must refuse');
		assert.deepEqual(calls, []);
		m.setStoredEngineChoice('rust');
		m.restartToLoadEngine();
		assert.deepEqual(calls, [['assign', 'http://h/performance']], 'the ?engine= override would outrank the save');
		const overlay = readFileSync(new URL('../../src/lib/components/settings/SettingsOverlay.svelte', import.meta.url), 'utf8');
		assert.match(overlay, /restartToLoadEngine\(\);/, 'the Settings button no longer runs the shared command');
		assert.doesNotMatch(overlay, /searchParams\.delete\('engine'\)/, 'the button keeps a private copy of the restart');
	} finally {
		Object.assign(m.rustMode, priorState);
		globalThis.window = prior;
	}
});

test('the engine bridge exists on every route, before the performance page commits an engine', async () => {
	// Sol P1s, PR #6829: first installed from initRustMode() (performance only),
	// then from rust-mode's own load, which arrives only with the deferred
	// Settings prefetch. Now a tiny eager module the root layout imports.
	const prior = globalThis.window;
	const calls = [];
	globalThis.window = {
		localStorage: { getItem: () => null, setItem: () => {} },
		location: { href: 'http://h/library', search: '', assign: (h) => calls.push(h), reload: () => calls.push('reload') }
	};
	try {
		await loadTypeScriptModule('src/lib/audio-engine/engine-bridge.ts');
		const bridge = globalThis.window.__mdtAudioEngine;
		assert.ok(bridge, 'window.__mdtAudioEngine is missing once the eager bridge module loads');
		assert.equal(await bridge.restartNeeded(), false, 'a page with no engine asked for a restart');
		// restartToLoadEngine() is the only source of this error, so the bridge runs the button's restart.
		await assert.rejects(
			() => bridge.restartToLoad(),
			/^Error: restartToLoadEngine: this page runs no engine/,
			'the agent bridge runs a different restart than the Settings button'
		);
		assert.deepEqual(calls, []);
		const layout = readFileSync(new URL('../../src/routes/+layout.svelte', import.meta.url), 'utf8');
		assert.match(layout, /^\timport '\$lib\/audio-engine\/engine-bridge';$/m, 'the root layout must load the bridge eagerly');
	} finally {
		globalThis.window = prior;
	}
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
