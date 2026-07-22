import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ipc;

before(async () => {
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
});

test('queue scopes isolate deck loads and coordinate only sync-sensitive commands', () => {
	assert.deepEqual(ipc.PERFORMANCE_PRESET_COMMAND_SCOPES, [1, 2, 3, 4, 'sync', 'headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'load', deck: 1, stable_id: 'a' }), [
		1
	]);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'pitch_range', deck: 1, range: 8 }),
		[1]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'quantize', deck: 2, enabled: false }),
		[2]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'beat_loop', deck: 2, beats: 4 }),
		[2]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({
			type: 'stem_mute',
			deck: 2,
			stem: 'instrumental',
			muted: true
		}),
		[2]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'seek', deck: 3, position_ms: 1000 }),
		[3, 'sync']
	);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'master', deck: 4 }), [4, 'sync']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'key_sync', deck: 4 }), [4, 'sync']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'channel_cue', deck: 4, enabled: true }), [4]);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_outputs_refresh' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_output_select', device_id: 'usb' }), ['headphone']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'key_nudge', deck: 4, semitones: -1 }), [4, 'sync']);
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'slip', deck: 4, enabled: true }), [4]);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'trim', deck: 2, value: 0.7 }), null);
	assert.equal(ipc.performanceCommandQueueScopes({ type: 'crossfader', value: 0.3 }), null);
});

test('key controls validate through IPC and round-trip serializable shift state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.key, null);
		assert.equal(initial.key_shift_semitones, 0);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'key_nudge', deck: 1, semitones: 2 }),
			/semitones must be -1 or 1/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'key_sync', deck: 1, extra: true }),
			/unexpected fields/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('SLIP validates through IPC and exposes its inactive read state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.slip_enabled, false);
		assert.equal(initial.slip_active, false);
		assert.equal(initial.slip_position_ms, null);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'slip', deck: 1, enabled: 'yes' }),
			/enabled must be boolean/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('deck header key controls dispatch only through the typed performance dispatcher', async () => {
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const headerSource = await readFile('src/lib/components/rb/deck/DeckHeader.svelte', 'utf8');
	assert.match(deckSource, /runPerformanceCommandFromUi\(\{ type: 'key_sync', deck: deckId \}\)/);
	assert.match(deckSource, /type: 'key_nudge', deck: deckId, semitones/);
	assert.match(headerSource, /onKeySync/);
	assert.match(headerSource, /onKeyNudge/);
	assert.match(headerSource, /aria-label="lower key by one semitone"/);
	assert.match(headerSource, /aria-label="raise key by one semitone"/);
	assert.match(deckSource, /candidate !== deckId/);
});

test('stem commands are strict typed IPC and default state never claims artifacts exist', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const state = ipc.queryPerformanceState();
		assert.equal(state.decks[1].stems.status, 'unavailable');
		assert.deepEqual(state.decks[1].stems.controls, {
			vocal: { muted: false, solo: false },
			instrumental: { muted: false, solo: false },
			drums: { muted: false, solo: false }
		});

		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'stem_mute',
				deck: 1,
				stem: 'mix',
				muted: true
			}),
			/stem must be vocal, instrumental, or drums/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'stem_solo',
				deck: 1,
				stem: 'vocal',
				solo: 'yes'
			}),
			/solo must be boolean/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('jog SLIP control dispatches only through the typed performance dispatcher', async () => {
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const jogSource = await readFile('src/lib/components/rb/deck/JogDial.svelte', 'utf8');
	assert.match(deckSource, /type: 'slip',[\s\S]*enabled: !deck\.slip_enabled/);
	assert.match(jogSource, /onSlip/);
	assert.match(jogSource, /data-performance-control="slip"/);
});

test('continuous mixer controls execute through IPC immediately and round-trip in query state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await ipc.dispatchPerformanceCommand({ type: 'trim', deck: 2, value: 0.7 });
		await ipc.dispatchPerformanceCommand({ type: 'eq', deck: 2, band: 'mid', value: 0.25 });
		await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 0.8 });
		await ipc.dispatchPerformanceCommand({ type: 'assign', deck: 2, assign: 'THRU' });
		await ipc.dispatchPerformanceCommand({ type: 'channel_cue', deck: 2, enabled: true });
		await ipc.dispatchPerformanceCommand({ type: 'crossfader', value: 0.3 });
		await ipc.dispatchPerformanceCommand({ type: 'master_volume', value: 0.6 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_mix', value: 0.25 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_level', value: 0.75 });

		const state = ipc.queryPerformanceState();
		assert.equal(state.command_pending, false);
		assert.equal(state.command_queued, 0);
		assert.deepEqual(state.mixer, {
		crossfader: 0.3,
		master: 0.6,
		headphones: {
			mix: 0.25,
			level: 0.75,
			selected_output_device_id: null,
			outputs: [],
			supported: false,
			active: false,
			error: null
		},
		channels: {
			1: {
				deck_id: 1,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				fader: 1,
				assign: 'A',
				cue_enabled: false
			},
			2: {
				deck_id: 2,
				trim: 0.7,
				eq_high: 0.5,
				eq_mid: 0.25,
				eq_low: 0.5,
				fader: 0.8,
				assign: 'THRU',
				cue_enabled: true
			},
			3: {
				deck_id: 3,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				fader: 1,
				assign: 'A',
				cue_enabled: false
			},
			4: {
				deck_id: 4,
				trim: 0.5,
				eq_high: 0.5,
				eq_mid: 0.5,
				eq_low: 0.5,
				fader: 1,
				assign: 'B',
				cue_enabled: false
			}
		}
		});

		await ipc.dispatchPerformanceCommand({ type: 'trim', deck: 2, value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'eq', deck: 2, band: 'mid', value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'fader', deck: 2, value: 1 });
		await ipc.dispatchPerformanceCommand({ type: 'assign', deck: 2, assign: 'B' });
		await ipc.dispatchPerformanceCommand({ type: 'channel_cue', deck: 2, enabled: false });
		await ipc.dispatchPerformanceCommand({ type: 'crossfader', value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'master_volume', value: 1 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_mix', value: 0.5 });
		await ipc.dispatchPerformanceCommand({ type: 'headphone_level', value: 0.5 });
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('mixer headphone controls use the typed dispatcher from every visible control', async () => {
	const [mixer, strip, headphones] = await Promise.all([
		readFile('src/lib/components/rb/Mixer.svelte', 'utf8'),
		readFile('src/lib/components/rb/mixer/ChannelStrip.svelte', 'utf8'),
		readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8')
	]);
	assert.match(mixer, /type: 'channel_cue'/);
	assert.match(mixer, /type: 'headphone_mix'/);
	assert.match(mixer, /type: 'headphone_level'/);
	assert.match(mixer, /type: 'headphone_outputs_refresh'/);
	assert.match(mixer, /type: 'headphone_output_select'/);
	assert.match(strip, /aria-pressed=\{cueEnabled\}/);
	assert.match(headphones, /aria-label="headphone output device"/);
});

test('uninstall invalidates retained IPC dispatchers and a new route session remains usable', async () => {
	globalThis.window = {};
	try {
		const uninstallOldSession = ipc.installPerformanceBrowserIpc();
		const staleDispatch = window.musicDjToolsPerformance.dispatch;
		uninstallOldSession();

		await assert.rejects(
			staleDispatch({ type: 'master_volume', value: 0.2 }),
			/performance command session .* invalidated/i
		);
		assert.equal(ipc.queryPerformanceState().mixer.master, 1);

		const uninstallNewSession = ipc.installPerformanceBrowserIpc();
		await window.musicDjToolsPerformance.dispatch({ type: 'master_volume', value: 0.4 });
		const state = ipc.queryPerformanceState();
		assert.equal(state.mixer.master, 0.4);
		assert.equal(state.command_pending, false);
		assert.equal(state.command_queued, 0);
		await window.musicDjToolsPerformance.dispatch({ type: 'master_volume', value: 1 });
		uninstallNewSession();
	} finally {
		delete globalThis.window;
	}
});

test('uninstall cannot invalidate a session after browser IPC ownership changes', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	const ownedIpc = window.musicDjToolsPerformance;
	const foreignIpc = { version: 1 };
	window.musicDjToolsPerformance = foreignIpc;
	try {
		assert.throws(uninstall, /ownership changed before cleanup/i);
		assert.equal(window.musicDjToolsPerformance, foreignIpc);
		await ownedIpc.dispatch({ type: 'master_volume', value: 0.4 });
		assert.equal(ipc.queryPerformanceState().mixer.master, 0.4);
		await ownedIpc.dispatch({ type: 'master_volume', value: 1 });
	} finally {
		window.musicDjToolsPerformance = ownedIpc;
		uninstall();
		delete globalThis.window;
	}
});
