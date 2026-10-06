/**
 * AGENT-20: AutoPlay's own state reaches the UI mirror, and an agent can switch it.
 *
 * Bug #2c (Tue 6 Oct 2026, silver preview soak): the soak agent could read the
 * decks and a PLAY-08 stall but not whether AutoPlay was on, nor whether it
 * would hand off. The classifier is pure and tested by matrix; the live half
 * RUNS the real controller and the real command bus (load-rune-module.mjs).
 *
 * Regression lines:
 *   [if] a disabled AutoPlay publishes a null reason [then] "off" reads like "armed" - broken
 *   [if] armed is reported with no playing master [then] agents wait for a handoff that never comes - broken
 *   [if] an active stall still reads armed [then] the stall banner and the mirror disagree - broken
 *   [if] {type:'autoplay'} does not flip uiPrefs [then] the agent switch is decorative - broken
 *   [if] the mirror omits the three fields or mirror_schema [then] the engine 422s or agents see nothing - broken
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import { installTimerProbe, loadRuneModule } from './load-rune-module.mjs';

let status;

before(async () => {
	status = await loadTypeScriptModule('src/lib/rb/autoplay-status.ts');
});

const ARMED_INPUT = {
	enabled: true,
	installed: true,
	stall_reason: null,
	has_source: true,
	handoff_pending: false,
	any_playing: true
};

test('a playing master with AutoPlay on is armed with no reason', () => {
	assert.deepEqual(status.classifyAutoPlayStatus(ARMED_INPUT), {
		autoplay_enabled: true,
		autoplay_armed: true,
		autoplay_disarm_reason: null
	});
});

test('each disarm reason, in the order an operator would fix them', () => {
	const cases = [
		[{ enabled: false }, false, 'disabled'],
		[{ installed: false }, true, 'not-installed'],
		[{ stall_reason: 'no-compatible-track' }, true, 'no-compatible-track'],
		[{ has_source: false }, true, 'no-playing-master'],
		[{ has_source: false, any_playing: false }, true, 'no-deck-playing'],
		// Precedence: disabled beats everything else that is also wrong.
		[{ enabled: false, installed: false, stall_reason: 'missing-audio' }, false, 'disabled'],
		[{ installed: false, stall_reason: 'missing-audio' }, true, 'not-installed']
	];
	for (const [override, enabled, reason] of cases) {
		assert.deepEqual(
			status.classifyAutoPlayStatus({ ...ARMED_INPUT, ...override }),
			{ autoplay_enabled: enabled, autoplay_armed: false, autoplay_disarm_reason: reason },
			JSON.stringify(override)
		);
	}
});

test('a settling handoff with no master yet still counts as armed', () => {
	const result = status.classifyAutoPlayStatus({
		...ARMED_INPUT,
		has_source: false,
		handoff_pending: true
	});
	assert.equal(result.autoplay_armed, true);
});

test('the invariant holds over the whole input matrix: reason null exactly when armed', () => {
	let seen = 0;
	for (const enabled of [true, false])
		for (const installed of [true, false])
			for (const stall_reason of [null, 'handoff-incomplete'])
				for (const has_source of [true, false])
					for (const handoff_pending of [true, false])
						for (const any_playing of [true, false]) {
							const out = status.classifyAutoPlayStatus({
								enabled, installed, stall_reason, has_source, handoff_pending, any_playing
							});
							seen += 1;
							assert.equal(out.autoplay_armed, out.autoplay_disarm_reason === null);
							assert.equal(out.autoplay_enabled, enabled);
							if (out.autoplay_armed) assert.equal(enabled, true);
							if (!enabled) assert.equal(out.autoplay_disarm_reason, 'disabled');
						}
	assert.equal(seen, 64, 'control: the matrix was actually walked');
});

// ----- live: the real controller, the real command bus ----------------------

const ENTRY = [
	"export { installAutoPlay, readAutoPlayMirrorStatus } from '$lib/rb/auto-play.svelte';",
	"export { setAutoPlayEnabled, uiPrefs } from '$lib/rb/prefs.svelte';",
	"export { deckStates } from '$lib/rb/audio-engine.svelte';",
	"export { executeAgentOrder } from '$lib/rb/agent-orders';",
	"export { installPerformanceBrowserIpc } from '$lib/rb/performance-ipc.svelte';"
].join('\n');

test('RUNNING it: the autoplay command switches AutoPlay and the next mirror read follows', async () => {
	const probe = installTimerProbe();
	let uninstall = null;
	let uninstallIpc = null;
	const hadWindow = 'window' in globalThis;
	try {
		const live = await loadRuneModule(ENTRY);
		assert.deepEqual(
			live.readAutoPlayMirrorStatus(),
			{ autoplay_enabled: true, autoplay_armed: false, autoplay_disarm_reason: 'not-installed' },
			'before /performance mounts the controller, on is not armed'
		);
		uninstall = live.installAutoPlay();
		await probe.flush();
		assert.equal(live.readAutoPlayMirrorStatus().autoplay_disarm_reason, 'no-deck-playing');

		const deck = live.deckStates[1];
		Object.assign(deck, { stable_id: 'sid-agent-20', playing: true, duration_ms: 600_000, position_ms: 0 });
		assert.equal(live.readAutoPlayMirrorStatus().autoplay_disarm_reason, 'no-playing-master');
		deck.is_master = true;
		assert.deepEqual(live.readAutoPlayMirrorStatus(), {
			autoplay_enabled: true,
			autoplay_armed: true,
			autoplay_disarm_reason: null
		});

		// The agent order runs inside the page's command session, as on /performance.
		globalThis.window ??= {};
		uninstallIpc = live.installPerformanceBrowserIpc();
		// PLAY-18: an off without user provenance is refused and changes nothing.
		const unowned = await live.executeAgentOrder({ kind: 'single', payload: { type: 'autoplay', enabled: false } });
		assert.equal(unowned.steps[0].status, 'failed', 'if an agent can switch AutoPlay off with no user ask then broken');
		assert.match(unowned.steps[0].error, /only a user turns AutoPlay off; send by_user: true when a person asked for this/);
		assert.equal(live.uiPrefs.auto_play_enabled, true);
		// The IPC bridge is app-internal: not applied, AutoPlay stays on, one WARN.
		const realWarn = console.warn;
		const warned = [];
		console.warn = (...args) => warned.push(args.join(' '));
		try {
			await globalThis.window.musicDjToolsPerformance.dispatch({ type: 'autoplay', enabled: false });
		} finally {
			console.warn = realWarn;
		}
		assert.equal(live.uiPrefs.auto_play_enabled, true, 'if the bridge can switch AutoPlay off with no user then broken');
		assert.match(warned.join('\n'), /AutoPlay off without user provenance not applied, AutoPlay stays on .*caller: window\.musicDjToolsPerformance/);

		const off = await live.executeAgentOrder({
			kind: 'single',
			payload: { type: 'autoplay', enabled: false, by_user: true }
		});
		assert.deepEqual(off.steps, [{ status: 'succeeded' }]);
		assert.deepEqual(off.mirror_delta.changed.ui, { auto_play_enabled: false });
		assert.equal(live.uiPrefs.auto_play_enabled, false);
		await probe.flush();
		assert.equal(probe.liveIntervals(), 0, 'off through the bus stops the poll, same as the button');
		assert.deepEqual(live.readAutoPlayMirrorStatus(), {
			autoplay_enabled: false,
			autoplay_armed: false,
			autoplay_disarm_reason: 'disabled'
		});

		const on = await live.executeAgentOrder({ kind: 'single', payload: { type: 'autoplay', enabled: true } });
		assert.deepEqual(on.mirror_delta.changed.ui, { auto_play_enabled: true });
		await probe.flush();
		assert.equal(live.readAutoPlayMirrorStatus().autoplay_armed, true);

		const bad = await live.executeAgentOrder({ kind: 'single', payload: { type: 'autoplay', enabled: 'yes' } });
		assert.equal(bad.steps[0].status, 'failed', 'a non-boolean switch is refused, not coerced');
		const extra = await live.executeAgentOrder({
			kind: 'single',
			payload: { type: 'autoplay', enabled: false, deck: 1 }
		});
		assert.equal(extra.steps[0].status, 'failed', 'an unknown field is refused');
		assert.equal(live.uiPrefs.auto_play_enabled, true, 'neither refused order changed anything');
		Object.assign(deck, { stable_id: null, playing: false, is_master: false });
	} finally {
		if (uninstallIpc !== null) uninstallIpc();
		if (uninstall !== null) uninstall();
		if (!hadWindow) delete globalThis.window;
		probe.restore();
	}
});

// ----- the publisher carries them --------------------------------------------

const MIRROR = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/ui-mirror.ts', import.meta.url)),
	'utf8'
);

test('buildUiMirror publishes the three fields from the live reader and declares schema 2', () => {
	const build = MIRROR.slice(
		MIRROR.indexOf('export function buildUiMirror'),
		MIRROR.indexOf('export function installUiMirror')
	);
	assert.ok(build.length > 0, 'control: buildUiMirror was found');
	assert.match(build, /const autoPlay = readAutoPlayMirrorStatus\(\);/);
	for (const key of ['autoplay_enabled', 'autoplay_armed', 'autoplay_disarm_reason']) {
		assert.match(build, new RegExp(`\\b${key}: autoPlay\\.${key},`), key);
	}
	assert.match(build, /mirror_schema: UI_MIRROR_SCHEMA,/);
	assert.match(MIRROR, /^export const UI_MIRROR_SCHEMA = 2;$/m);
	assert.doesNotMatch(build, /autoplay_enabled: true/, 'control: no hardcoded value');
});
