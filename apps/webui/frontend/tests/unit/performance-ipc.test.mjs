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
	assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_output_acquire' }), ['headphone']);
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
			window.musicDjToolsPerformance.dispatch({ type: 'key_sync', deck: 1, enabled: true, extra: true }),
			/unexpected fields/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({ type: 'key_sync', deck: 1 }),
			/missing keys|enabled/i
		);
		assert.equal(initial.key_sync_enabled, false);
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

test('hot-cue controls are strict IPC commands with serializable slot state', async () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.hot_cue_slots.length, 8);
		assert.deepEqual(initial.hot_cue_slots.map((slot) => slot.slot), ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']);
		assert.ok(initial.hot_cue_slots.every((slot) => typeof slot.revision === 'string'));
		assert.equal(initial.hot_cue_reversal, null);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_save', deck: 1, slot: 'I', in_ms: 1000, revision: 'etag'
			}),
			/hot-cue slot must be A through H/i
		);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_restore', deck: 1, slot: 'A', revision: 'etag', reversal_id: 'token', extra: true
			}),
			/unexpected fields/i
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
	const [deckSource, ipcSource] = await Promise.all([
		readFile('src/lib/components/rb/Deck.svelte', 'utf8'),
		readFile('src/lib/rb/performance-ipc.svelte.ts', 'utf8')
	]);
	assert.match(deckSource, /type: 'hot_cue_save', deck: deckId, slot, in_ms: ms, revision/);
	assert.match(deckSource, /type: 'hot_cue_restore', deck: deckId, slot, revision, reversal_id: reversalId/);
	assert.match(ipcSource, /await saveHotCue\(stableId, command\.slot, command\.in_ms, command\.revision\)/);
	assert.match(ipcSource, /await restoreHotCue\(stableId, command\.slot, command\.revision, command\.reversal_id\)/);
});

test('hot-cue IPC dispatch sends CAS revisions and exposes one-time reversal state', async () => {
	const originalFetch = globalThis.fetch;
	const requests = [];
	let restoreCalls = 0;
	globalThis.fetch = async (input, init = {}) => {
		requests.push({ url: String(input), init });
		if (String(input).endsWith('/restore')) {
			restoreCalls += 1;
			if (restoreCalls > 1) {
				return Response.json(
					{ detail: { code: 'HOT_CUE_REVERSAL_CONSUMED', message: 'already used' } },
					{ status: 409 }
				);
			}
			return Response.json({ cue: null, revision: 'restore-revision' });
		}
		if (init.method === 'DELETE') {
			return Response.json({
				cue: null,
				revision: 'clear-revision',
				reversal: { reversal_id: 'clear-token' }
			});
		}
		return Response.json({
			cue: { slot: 'A', revision: 'save-revision' },
			revision: 'save-revision',
			reversal: { reversal_id: 'save-token' }
		});
	};
	globalThis.window = {};
	const resetDriver = ipc.installPerformanceHotCueDriverForTest({
		stableId: () => 'loaded-track',
		refresh: async () => {},
		hasRbMapping: () => true
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const saved = await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 1000, revision: 'empty-revision'
		});
		assert.deepEqual(saved.decks[1].hot_cue_reversal, {
			slot: 'A', revision: 'save-revision', reversal_id: 'save-token'
		});
		await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_clear', deck: 1, slot: 'A', revision: 'save-revision'
		});
		const restored = await window.musicDjToolsPerformance.dispatch({
			type: 'hot_cue_restore', deck: 1, slot: 'A', revision: 'clear-revision', reversal_id: 'clear-token'
		});
		assert.equal(restored.decks[1].hot_cue_reversal, null);
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_restore', deck: 1, slot: 'A', revision: 'clear-revision', reversal_id: 'clear-token'
			}),
			/HOT_CUE_REVERSAL_CONSUMED/i
		);
		assert.equal(requests[0].init.headers['If-Match'], 'empty-revision');
		assert.equal(requests[1].init.headers['If-Match'], 'save-revision');
		assert.equal(requests[2].init.headers['If-Match'], 'clear-revision');
		assert.equal(requests[3].init.headers['If-Match'], 'clear-revision');
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
		globalThis.fetch = originalFetch;
	}
});

test('hot_cue_save rejects for an unmapped deck before reaching the network, same as HotCueBank (#736)', async () => {
	const originalFetch = globalThis.fetch;
	let fetchCalls = 0;
	globalThis.fetch = async () => {
		fetchCalls += 1;
		throw new Error('saveHotCue must not reach the network for an unmapped deck');
	};
	globalThis.window = {};
	const resetDriver = ipc.installPerformanceHotCueDriverForTest({
		stableId: () => 'loaded-track',
		refresh: async () => {},
		hasRbMapping: () => false
	});
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		await assert.rejects(
			window.musicDjToolsPerformance.dispatch({
				type: 'hot_cue_save', deck: 1, slot: 'A', in_ms: 1000, revision: 'etag'
			}),
			/no live rekordbox mapping/i
		);
		assert.equal(fetchCalls, 0, 'hot_cue_save must reject before calling saveHotCue');
	} finally {
		uninstall();
		resetDriver();
		delete globalThis.window;
		globalThis.fetch = originalFetch;
	}
});

test('queryPerformanceState serializes has_rb_mapping so a browser/CLI agent can gate on it (#736)', () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const initial = ipc.queryPerformanceState().decks[1];
		assert.equal(initial.has_rb_mapping, true, 'a fresh unloaded deck defaults has_rb_mapping true');
	} finally {
		uninstall();
		delete globalThis.window;
	}
});

test('deck header key controls dispatch only through the typed performance dispatcher', async () => {
	const deckSource = await readFile('src/lib/components/rb/Deck.svelte', 'utf8');
	const headerSource = await readFile('src/lib/components/rb/deck/DeckHeader.svelte', 'utf8');
	assert.match(
		deckSource,
		/runPerformanceCommandFromUi\(\{\s*type: 'key_sync',\s*deck: deckId,\s*enabled: !deck\.key_sync_enabled\s*\}\)/
	);
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

test('jog live BPM uses PQTZ playbackBpm, not tag BPM times pitch', async () => {
	const jogSource = await readFile('src/lib/components/rb/deck/JogDial.svelte', 'utf8');
	assert.match(jogSource, /playbackBpm/);
	assert.doesNotMatch(
		jogSource,
		/deck\.bpm === null \? null : deck\.bpm \* deck\.pitch/
	);
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
	assert.match(mixer, /type: 'headphone_output_acquire'/);
	assert.match(mixer, /type: 'headphone_output_select'/);
	assert.match(headphones, /onclick=\{onacquire\}/);
	assert.match(headphones, /Grant browser access to a second audio output/);
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

//-----------------------------------------------------------------------------
// agent-native parity for the toast tray
//-----------------------------------------------------------------------------

/**
 * Every pointer interaction the toast tray offers must have an equal here, or
 * an agent cannot drive and assert the same flows a human can.
 *
 * Behavior lives in toast-behavior.test.mjs against the store; what is pinned
 * here is that the bridge EXPOSES it, validates its input like every other IPC
 * entry point, and delegates rather than reimplementing.
 *
 * Regression lines:
 * - if a tray method disappears from the IPC then agent parity silently lapses
 *   and only a human can dismiss a stuck toast
 * - if an id reaches a store lookup unvalidated then the untyped bridge is the
 *   one input path that skips the validation every other one performs
 * - if a method reimplements the behavior instead of delegating then the pointer
 *   and the agent can drift apart
 */
test('the IPC exposes an equal for every toast interaction a pointer has', () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const bridge = globalThis.window.musicDjToolsPerformance;
		for (const method of ['toasts', 'dismissToast', 'holdToast', 'releaseToast', 'copyToast']) {
			assert.equal(typeof bridge[method], 'function', `${method} must be reachable by an agent`);
		}
		assert.ok(Array.isArray(bridge.toasts()), 'toasts() lists what is on screen');
	} finally {
		uninstall();
	}
});

test('a toast id arriving over the untyped bridge is validated, not trusted', () => {
	globalThis.window = {};
	const uninstall = ipc.installPerformanceBrowserIpc();
	try {
		const bridge = globalThis.window.musicDjToolsPerformance;
		for (const method of ['dismissToast', 'holdToast', 'releaseToast']) {
			for (const bad of [undefined, null, 42, '', {}]) {
				assert.throws(
					() => bridge[method](bad),
					TypeError,
					`${method} must refuse ${JSON.stringify(bad) ?? 'undefined'}`
				);
			}
		}
	} finally {
		uninstall();
	}
});

test('dismissing a toast is NOT gated on the performance command session', async () => {
	// A toast outlives any one command session. Gating it would mean a preset
	// transaction moving on could leave a stuck error undismissable, which is
	// the bug the dismiss control exists to fix.
	const source = await readFile(
		new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url),
		'utf8'
	);
	const block = source.slice(source.indexOf('dismissToast: (id: unknown)'));
	const nextMethod = block.indexOf('copyToast:');
	assert.equal(
		/_assertCommandSession/.test(block.slice(0, nextMethod)),
		false,
		'no toast method may sit behind the command-session gate'
	);
	assert.match(
		source,
		/dismissToast: \(id: unknown\) => dismissToast\(_toastId\(id\)\)/,
		'the bridge must delegate to the store, not reimplement dismissal'
	);
	assert.match(source, /copyToast: \(id: unknown\) => copyToast\(_toastId\(id\)\)/);
});

test('load play intent is strictly validated, immediate, and visible to agents', async () => {
	const source = await readFile(
		new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url),
		'utf8'
	);
	assert.match(source, /type: 'load_play_intent'; deck: DeckId; generation: number; desired_play: boolean/);
	assert.match(source, /_exactKeys\(record, \['type', 'deck', 'generation', 'desired_play'\]\)/);
	assert.match(source, /generation must be a positive safe integer/);
	assert.match(source, /command\.type === 'load_play_intent'[\s\S]{0,120}return null/);
	assert.match(source, /load_play_intent: Record<DeckId, \{ generation: number; desired_play: boolean \} \| null>/);
});
