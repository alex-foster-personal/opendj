// requirement: BEATFX-13992
// Beat FX X-PAD strip state machine through the real typed dispatcher (issue #13978, DJM-900NXS2 p. 14 mimicked).
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let uninstall;
let sent = [];

/** Run one X-PAD step's command through `runPerformanceCommandFromUi`, recording what was sent. */
async function play(step) {
	const reason = await m.panel.runBeatFxPanelCommand((command) => {
		sent.push(command);
		return m.runPerformanceCommandFromUi(command);
	}, step.command);
	assert.equal(reason, null, JSON.stringify(step.command));
	return step;
}

/** Put the unit back to off at 1 beat with fx.beat_rack on, and clear the send log. */
async function reset() {
	m.buildFlags.flags = [{ flag_id: 'fx.beat_rack', enabled: true }];
	const result = await m.runPerformanceCommandFromUi({
		type: 'beat_fx',
		effect: 'echo',
		beats: 1,
		enabled: false
	});
	assert.deepEqual(result, { ok: true });
	sent = [];
}

/** The unit fields an X-PAD can move, for comparing two runs. */
function onAndBeats() {
	const view = m.beatFxView();
	return [view.enabled, view.beats];
}

before(async () => {
	m = await loadTypeScriptModule('tests/unit/fixtures/beat-fx-panel-entry.ts', {
		viteApiBase: 'http://127.0.0.1:9'
	});
	globalThis.window = {};
	uninstall = m.installPerformanceBrowserIpc();
});

after(() => {
	uninstall();
	delete globalThis.window;
});

test('X-PAD pointer down sends beat_fx enabled true at the division under the pointer', async () => {
	// [if] X-PAD pointer down does not send beat_fx enabled true with the division under the finger [then] touch plays nothing, [else stop].
	await reset();
	const x = m.panel.beatFxXpadPosition(105, 100, 100);
	const step = await play(m.panel.beatFxXpadDown(m.beatFxView(), x));
	assert.deepEqual(sent, [{ type: 'beat_fx', beats: 1 / 16, enabled: true }]);
	assert.deepEqual(onAndBeats(), [true, 1 / 16]);
	assert.deepEqual([step.touch.touching, step.touch.wasOn], [true, false]);
	assert.equal(m.panel.beatFxXpadPosition(250, 100, 100), 1, 'clamped at the right edge');
	assert.throws(() => m.panel.beatFxXpadPosition(0, 0, 0), /width must be > 0/);
});

test('X-PAD slide while held changes the division and pointer up sends enabled false', async () => {
	// [if] an X-PAD slide does not move the division, resends an unchanged one, or release leaves it on [then] X-PAD is stuck, [else stop].
	await reset();
	let step = await play(m.panel.beatFxXpadDown(m.beatFxView(), 0.05));
	step = await play(m.panel.beatFxXpadMove(step.touch, 0.65));
	assert.equal(m.beatFxView().beats, 2);
	assert.equal(
		m.panel.beatFxXpadMove(step.touch, 0.66).command,
		null,
		'same division sends nothing'
	);
	step = await play(m.panel.beatFxXpadUp(step.touch));
	assert.deepEqual(sent.at(-1), { type: 'beat_fx', enabled: false });
	assert.deepEqual(onAndBeats(), [false, 2]);
	assert.equal(step.touch, m.panel.XPAD_IDLE);
	assert.equal(m.panel.beatFxXpadMove(step.touch, 0.1).command, null, 'no move without a touch');
	assert.equal(m.panel.beatFxXpadUp(step.touch).command, null, 'no release without a touch');
});

test('ON/OFF pressed during an X-PAD touch latches, so pointer up leaves the effect on', async () => {
	// [if] ON/OFF during an X-PAD touch does not latch, or release turns off an effect already on before the touch [then] latch is broken, [else stop].
	await reset();
	let step = await play(m.panel.beatFxXpadDown(m.beatFxView(), 0.5));
	step = await play(m.panel.beatFxOnOffPress(m.beatFxView(), step.touch));
	assert.equal(step.touch.latched, true);
	step = await play(m.panel.beatFxXpadUp(step.touch));
	assert.equal(step.command, null);
	assert.equal(m.beatFxView().enabled, true, 'latched on');
	step = await play(m.panel.beatFxXpadDown(m.beatFxView(), 0.1));
	await play(m.panel.beatFxXpadUp(step.touch));
	assert.equal(m.beatFxView().enabled, true, 'on before the touch stays on');
	await play(m.panel.beatFxOnOffPress(m.beatFxView(), m.panel.XPAD_IDLE));
	assert.equal(m.beatFxView().enabled, false, 'ON/OFF with no touch toggles');
});

test('an agent reaches every X-PAD state with plain beat_fx commands over the IPC bridge', async () => {
	// [if] an X-PAD command carries panel-only fields or the agent IPC replay ends in a different unit state [then] UI and agents diverge, [else stop].
	await reset();
	const views = [];
	let step = await play(m.panel.beatFxXpadDown(m.beatFxView(), 0.2));
	views.push(onAndBeats());
	step = await play(m.panel.beatFxXpadMove(step.touch, 0.75));
	views.push(onAndBeats());
	await play(m.panel.beatFxXpadUp(step.touch));
	views.push(onAndBeats());
	const commands = sent;
	for (const command of commands) {
		assert.deepEqual(m.parseBeatFxCommand(command), command);
		assert.equal('xpad' in command, false, 'the unit ignores xpad, so the panel never sends it');
	}
	await reset();
	const replayed = [];
	for (const command of commands) {
		await globalThis.window.musicDjToolsPerformance.dispatch(command);
		replayed.push(onAndBeats());
	}
	assert.deepEqual(replayed, views);
	assert.deepEqual(views, [
		[true, 1 / 4],
		[true, 4],
		[false, 4]
	]);
});
