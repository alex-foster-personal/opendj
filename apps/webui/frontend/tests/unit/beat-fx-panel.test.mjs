// requirement: BEATFX-13972
// Beat FX panel logic through the real typed dispatcher (issue #9785, spec fx-fanout-20261108/01 sections 4.3, 4.4).
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;
let uninstall;

/** Set `fx.beat_rack` on the shared flag store. */
function setRack(on) {
	m.buildFlags.flags = [{ flag_id: 'fx.beat_rack', enabled: on }];
}

/** Run one panel command through `runPerformanceCommandFromUi`; resolves to the refusal or null. */
function send(command) {
	return m.panel.runBeatFxPanelCommand(m.runPerformanceCommandFromUi, command);
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

test('the FX button opens the panel only with fx.beat_rack on, else it keeps the planned explainer', () => {
	// [if] the panel gate ignores fx.beat_rack or the flag-off button loses its planned explainer [then] FX opens unbuilt, [else stop].
	setRack(true);
	assert.equal(m.beatFxRackEnabled(), true);
	assert.match(m.explainers.builtExplainerBullets('fx-panel')[0], /X-PAD/);
	setRack(false);
	assert.equal(m.beatFxRackEnabled(), false);
	assert.match(m.explainers.plannedTitle('fx'), /^FX panel - .*\(Not built yet\.\)$/);
});

test('the panel shows CH SELECT lamps, the division with its ms, ON blink and the three quantize states', async () => {
	// [if] CH SELECT, BEAT ms at 128 BPM, ON blink or QUANTIZE on/gray/hidden misreport the unit view [then] the panel lies, [else stop].
	setRack(true);
	assert.equal(
		await send({ type: 'beat_fx', target: '2', beats: 1, enabled: true, quantize: true }),
		null
	);
	const view = m.beatFxView();
	const lamps = m.panel.beatFxChannelLamps(view);
	assert.deepEqual(
		lamps.map((l) => l.label),
		['1', '2', '3', '4', 'M']
	);
	assert.deepEqual(
		lamps.filter((l) => l.lit).map((l) => [l.target, l.blinking]),
		[['2', true]]
	);
	assert.equal(m.panel.beatFxDivisionLabel(view.beats), '1');
	assert.equal(m.panel.beatFxTimeReadout(view), '469 ms');
	assert.deepEqual([1 / 16, 0.75, 16].map(m.panel.beatFxDivisionLabel), ['1/16', '3/4', '16']);
	assert.equal(m.panel.beatFxQuantizeLamp(view), 'on');
	assert.equal(
		m.panel.beatFxQuantizeLamp({ ...view, beat_ms: null, quantize_state: 'no_grid' }),
		'gray'
	);
	assert.equal(m.panel.beatFxTimeReadout({ ...view, beat_ms: null }), '-- ms');
	assert.equal(await send({ type: 'beat_fx', enabled: false, quantize: false }), null);
	const off = m.beatFxView();
	assert.equal(m.panel.beatFxQuantizeLamp(off), 'hidden');
	assert.equal(m.panel.beatFxChannelLamps(off).find((l) => l.lit).blinking, false);
});

test('every panel control builds a beat_fx command the dispatcher accepts and runs', async () => {
	// [if] a panel control builds a command beat_fx refuses, or its run does not show in the unit view [then] UI and agents diverge, [else stop].
	setRack(true);
	const view = m.beatFxView();
	const commands = [
		m.panel.beatFxTargetCommand('master'),
		m.panel.beatFxEffectCommand('echo'),
		m.panel.beatFxBeatStepCommand(view.beats, -1),
		m.panel.beatFxLevelCommand(0.3),
		m.panel.beatFxQuantizeCommand(view),
		m.panel.beatFxOnOffPress(view, m.panel.XPAD_IDLE).command
	];
	for (const command of commands) {
		assert.deepEqual(m.parseBeatFxCommand(command), command);
		assert.equal(await send(command), null, JSON.stringify(command));
	}
	const after = m.beatFxView();
	assert.deepEqual(
		[after.target, after.level, after.quantize, after.enabled],
		['master', 0.3, !view.quantize, !view.enabled]
	);
	assert.equal(m.panel.beatFxBeatStepCommand(16, 1), null, 'no step past 16 beats');
	assert.ok(m.panel.beatFxEffectOptions().some((o) => o.name === 'echo' && o.built));
});

test('a refused panel command returns its reason for the panel and changes nothing', async () => {
	// [if] a refused panel command shows no reason or changes the unit view [then] refusals are silent, [else stop].
	setRack(true);
	const before_off = m.beatFxView();
	setRack(false);
	const reason = await send(m.panel.beatFxTargetCommand('3'));
	assert.equal(reason, 'fx.beat_rack is off');
	setRack(true);
	assert.deepEqual(m.beatFxView(), before_off);
	const unbuilt = m.panel.beatFxEffectOptions().find((o) => !o.built);
	assert.ok(unbuilt, 'an unbuilt effect exists to refuse');
	assert.match(await send(m.panel.beatFxEffectCommand(unbuilt.name)), /kernel not built yet/);
	assert.deepEqual(m.beatFxView(), before_off);
});
