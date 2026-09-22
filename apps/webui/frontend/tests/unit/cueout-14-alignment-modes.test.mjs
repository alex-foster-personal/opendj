// requirement: CUEOUT-14 (alignment modes, master delay setter, persistence)
// [if] alignment mode is hybrid and offset is -30 ms [then] HEAD DELAY becomes 30 and master delay stays 0
// [if] alignment mode is hybrid and offset is +700 ms [then] master delay becomes 700 and HEAD DELAY becomes 0
// [if] alignment mode is headphones_only and offset is +700 ms [then] nothing is delayed and the warning names 700 ms
// [if] alignment mode is delay_all and offset is +2000 ms [then] master delay is capped at 1500 and the result says so
// [if] the app reloads [then] alignment_mode, master_delay_ms and last_calibration are restored
// [if] mode is switched after a calibration [then] delays are re-derived from last_calibration without re-measuring
// [if] a wired "External Headphones" cue device is selected [then] the calibrate button is enabled
// [if] setMasterDelayMs is handed a non-finite or out-of-range value [then] it throws and the previous value stands
// [if] an old blob that only carries head_delay_ms is loaded [then] the new fields default and nothing throws
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const MIXER_CONFIG_STORAGE_KEY = 'mdt.rb.mixer-config.v1';

/** Minimal localStorage + window so persistence paths are live. */
function installFakeWindow() {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

let cueAlign;
let constants;

before(async () => {
	installFakeWindow();
	cueAlign = await loadTypeScriptModule('src/lib/player/cue-align.svelte.ts');
	constants = await loadTypeScriptModule('src/lib/player/constants.ts');
});

describe('deriveAlignment (the mode table from the spec)', () => {
	test('hybrid, headphones 30 ms ahead: HEAD DELAY 30, room untouched', () => {
		const plan = cueAlign.deriveAlignment('hybrid', -30);
		assert.equal(plan.head_delay_ms, 30, 'if hybrid does not delay the phones for a negative offset then wired jack vs HDMI stays out of step - broken');
		assert.equal(plan.master_delay_ms, 0);
		assert.equal(plan.warning, null);
		assert.equal(plan.capped, false);
	});

	test('hybrid, headphones 700 ms behind: room delayed 700, HEAD DELAY 0', () => {
		const plan = cueAlign.deriveAlignment('hybrid', 700);
		assert.equal(plan.master_delay_ms, 700, 'if hybrid does not delay the room for a positive offset then Bluetooth phones stay 700 ms behind - broken');
		assert.equal(plan.head_delay_ms, 0);
		assert.equal(plan.warning, null);
	});

	test('headphones_only, 700 ms behind: nothing delayed, warning names 700 ms', () => {
		const plan = cueAlign.deriveAlignment('headphones_only', 700);
		assert.equal(plan.head_delay_ms, 0);
		assert.equal(plan.master_delay_ms, 0);
		assert.match(plan.warning, /700 ms behind/, 'if headphones_only delays nothing AND says nothing then the operator cannot tell why the phones lag - broken');
		assert.match(plan.warning, /alignment mode/i);
	});

	test('delay_all, 2000 ms behind: master delay capped at 1500 and the plan says so', () => {
		const plan = cueAlign.deriveAlignment('delay_all', 2000);
		assert.equal(plan.master_delay_ms, constants.MASTER_DELAY_MAX_MS);
		assert.equal(plan.master_delay_ms, 1500);
		assert.equal(plan.capped, true);
		assert.match(plan.warning, /capped at 1500/, 'if a capped delay is reported as a full fix then the operator trusts a room that is still 500 ms off - broken');
	});

	test('headphones_only and delay_all both cap HEAD DELAY at 500 for a large negative offset', () => {
		for (const mode of ['headphones_only', 'delay_all', 'hybrid']) {
			const plan = cueAlign.deriveAlignment(mode, -900);
			assert.equal(plan.head_delay_ms, constants.HEAD_DELAY_MAX_MS, `${mode} must cap at HEAD_DELAY_MAX_MS`);
			assert.equal(plan.master_delay_ms, 0);
			assert.equal(plan.capped, true);
		}
	});

	test('zero offset delays nothing in every mode', () => {
		for (const mode of cueAlign.HEADPHONE_ALIGNMENT_MODES) {
			assert.deepEqual(cueAlign.deriveAlignment(mode, 0), {
				head_delay_ms: 0,
				master_delay_ms: 0,
				warning: null,
				capped: false
			});
		}
	});

	test('an unknown mode or a non-finite offset throws rather than guessing', () => {
		assert.throws(() => cueAlign.deriveAlignment('mixxx', 10), /alignment_mode must be headphones_only, delay_all, or hybrid/);
		assert.throws(() => cueAlign.deriveAlignment('hybrid', Number.NaN), /offset/);
		assert.throws(() => cueAlign.deriveAlignment('hybrid', '10'), /offset/);
	});
});

describe('constants', () => {
	test('MASTER_DELAY_MAX_MS is 1500 and assertMasterDelayMs guards the closed range', () => {
		assert.equal(constants.MASTER_DELAY_MAX_MS, 1500);
		constants.assertMasterDelayMs(0);
		constants.assertMasterDelayMs(1500);
		for (const bad of [1501, -1, Number.NaN, Infinity, '40', null, undefined]) {
			assert.throws(() => constants.assertMasterDelayMs(bad), /master delay must be a finite number within 0\.\.1500/);
		}
		assert.equal(constants.masterDelaySeconds(1500), 1.5);
	});

	test('HEADPHONE_ALIGNMENT_MODES is the three-way table and hybrid is a member', () => {
		assert.deepEqual([...constants.HEADPHONE_ALIGNMENT_MODES], ['headphones_only', 'delay_all', 'hybrid']);
		constants.assertHeadphoneAlignmentMode('hybrid');
		assert.throws(() => constants.assertHeadphoneAlignmentMode('serato'), /alignment_mode must be headphones_only, delay_all, or hybrid; got serato/);
	});
});

describe('calibrate button gate', () => {
	test('External Headphones (the Mac jack) in two_outputs is calibratable - the old label skip is gone', () => {
		assert.equal(
			cueAlign.calibrateButtonEnabled({ output_mode: 'two_outputs', selected_output_device_id: 'wired-jack' }),
			true,
			'if the wired jack cannot be calibrated then the wired-vs-HDMI case the spec was written for is unreachable - broken'
		);
		assert.equal(cueAlign.calibrateButtonEnabled({ output_mode: 'two_outputs', selected_output_device_id: null }), false);
		assert.equal(cueAlign.calibrateButtonEnabled({ output_mode: 'practice', selected_output_device_id: 'x' }), false);
		assert.equal(cueAlign.calibrateButtonEnabled({ output_mode: 'split_cable', selected_output_device_id: 'x' }), false);
		assert.throws(() => cueAlign.calibrateButtonEnabled({ output_mode: 'nope', selected_output_device_id: 'x' }), /output_mode/);
	});

	test('headphones.ts no longer auto-calibrates on first select and no longer skips External Headphones', async () => {
		const source = await readFile('src/lib/player/headphones.ts', 'utf8');
		assert.doesNotMatch(source, /external headphones/i, 'if the label skip survives then a wired jack is never measured - broken');
		assert.doesNotMatch(source, /_maybeCalibrateCueLatency|_calibrateFirstSelectCueLatency|_calibratedCueIds|_calibratingCueId|shouldCalibrateCueLatency/,
			'if the first-select auto-calibration survives then a chirp fires into the phones on every device pick, beside the modal - broken');
	});
});

describe('mixer config persistence (alignment_mode, master_delay_ms, last_calibration)', () => {
	test('an old blob that only has head_delay_ms loads with the new defaults', async () => {
		const store = installFakeWindow();
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ head_delay_ms: 77 }));
		const mixerConfig = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
		assert.deepEqual(mixerConfig.loadMixerConfig(), {
			head_delay_ms: 77,
			alignment_mode: 'hybrid',
			master_delay_ms: 0,
			last_calibration: null
		});
	});

	test('a missing key yields defaults and a malformed new field throws with recovery guidance', async () => {
		const store = installFakeWindow();
		const mixerConfig = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
		assert.equal(mixerConfig.loadMixerConfig().alignment_mode, 'hybrid');
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ head_delay_ms: 0, master_delay_ms: 1501 }));
		assert.throws(() => mixerConfig.loadMixerConfig(), /malformed.*clear the localStorage key/i);
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ head_delay_ms: 0, alignment_mode: 'serato' }));
		assert.throws(() => mixerConfig.loadMixerConfig(), /malformed.*clear the localStorage key/i);
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ head_delay_ms: 0, last_calibration: { cue_latency_ms: 1 } }));
		assert.throws(() => mixerConfig.loadMixerConfig(), /malformed.*clear the localStorage key/i);
	});

	test('persistMixerConfig merges a partial patch over the stored keys and validates it', async () => {
		const store = installFakeWindow();
		store.set(MIXER_CONFIG_STORAGE_KEY, JSON.stringify({ head_delay_ms: 12 }));
		const mixerConfig = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
		mixerConfig.persistMixerConfig({ master_delay_ms: 700 });
		assert.deepEqual(JSON.parse(store.get(MIXER_CONFIG_STORAGE_KEY)), { head_delay_ms: 12, master_delay_ms: 700 },
			'if a partial patch drops the other keys then setting the room delay forgets HEAD DELAY - broken');
		assert.throws(() => mixerConfig.persistMixerConfig({ master_delay_ms: -1 }), /master delay/);
		assert.throws(() => mixerConfig.persistMixerConfig({ alignment_mode: 'mixxx' }), /alignment_mode/);
		assert.throws(() => mixerConfig.persistMixerConfig({ last_calibration: { cue_latency_ms: 1 } }), /last_calibration/);
	});

	test('reload restores alignment_mode, master_delay_ms and last_calibration', async () => {
		const store = installFakeWindow();
		const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
		const record = {
			cue_latency_ms: 900,
			master_latency_ms: 200,
			measured_at: '2026-09-15T20:00:00.000Z',
			cue_device_id: 'bt-1',
			master_device_id: 'lg-hdmi'
		};
		headphones.setAlignmentMode('delay_all');
		headphones.setMasterDelayMs(700);
		(await loadTypeScriptModule('src/lib/player/mixer-config.ts')).persistMixerConfig({ last_calibration: record });
		const reloaded = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
		assert.deepEqual(reloaded.loadMixerConfig(), {
			head_delay_ms: 0,
			alignment_mode: 'delay_all',
			master_delay_ms: 700,
			last_calibration: record
		});
		const state = await loadTypeScriptModule('src/lib/player/state.svelte.ts');
		const defaults = state._defaultHeadphones();
		assert.equal(defaults.alignment_mode, 'delay_all', 'if the mode is not restored then every reload silently reverts to hybrid - broken');
		assert.equal(defaults.master_delay_ms, 700, 'if the room delay is not restored then the reloaded room jumps 700 ms early - broken');
		assert.equal(defaults.head_delay_ms, 0);
		assert.deepEqual(defaults.calibration, {
			step: 'idle',
			cue_latency_ms: 900,
			master_latency_ms: 200,
			offset_ms: 700,
			error: null,
			diagnostics: {
				probe: 'chirp',
				alternate_probe: 'unavailable',
				failure: null,
				master_measurements_ms: [],
				cue_measurements_ms: [],
				spread_ms: null
			}
		});
	});
});

describe('setters', () => {
	test('setMasterDelayMs rejects invalid values and leaves the previous value', async () => {
		installFakeWindow();
		const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		headphones.setMasterDelayMs(40);
		for (const bad of [1501, -1, Number.NaN, Infinity, '40', null]) {
			assert.throws(() => headphones.setMasterDelayMs(bad), /master delay must be a finite number within 0\.\.1500/);
			assert.equal(ipc.queryPerformanceState().mixer.headphones.master_delay_ms, 40);
		}
		headphones.setMasterDelayMs(1500);
		assert.equal(ipc.queryPerformanceState().mixer.headphones.master_delay_ms, 1500);
		headphones.setMasterDelayMs(0);
	});

	test('setAlignmentMode re-derives both delays from last_calibration without re-measuring', async () => {
		const store = installFakeWindow();
		store.set(
			MIXER_CONFIG_STORAGE_KEY,
			JSON.stringify({
				head_delay_ms: 0,
				alignment_mode: 'hybrid',
				master_delay_ms: 700,
				last_calibration: {
					cue_latency_ms: 900,
					master_latency_ms: 200,
					measured_at: '2026-09-15T20:00:00.000Z',
					cue_device_id: 'bt-1',
					master_device_id: 'lg-hdmi'
				}
			})
		);
		const headphones = await loadTypeScriptModule('src/lib/player/headphones.ts');
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		// mixerState is a process-wide singleton (earlier tests already touched
		// it), so the persisted 700 is re-derived through the setter under test
		// rather than assumed from a fresh load.
		headphones.setAlignmentMode('hybrid');
		const before = ipc.queryPerformanceState().mixer.headphones;
		assert.equal(before.master_delay_ms, 700, 'hybrid re-derives the room delay from last_calibration');
		headphones.setAlignmentMode('headphones_only');
		const after = ipc.queryPerformanceState().mixer.headphones;
		assert.equal(after.alignment_mode, 'headphones_only');
		assert.equal(after.master_delay_ms, 0, 'if switching to headphones_only leaves the room delayed then the mode selector lies - broken');
		assert.equal(after.head_delay_ms, 0);
		headphones.setAlignmentMode('delay_all');
		assert.equal(ipc.queryPerformanceState().mixer.headphones.master_delay_ms, 700, 'if switching back does not re-derive from last_calibration then the operator has to re-measure - broken');
		assert.equal(JSON.parse(store.get(MIXER_CONFIG_STORAGE_KEY)).alignment_mode, 'delay_all');
		assert.throws(() => headphones.setAlignmentMode('serato'), /alignment_mode must be/);
		assert.equal(ipc.queryPerformanceState().mixer.headphones.alignment_mode, 'delay_all');
	});

	test('the alignment IPC commands parse, scope and reject like head_delay_ms', async () => {
		installFakeWindow();
		const ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts');
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'headphone_alignment_mode', value: 'hybrid' }), null);
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'master_delay_ms', value: 40 }), null);
		assert.deepEqual(ipc.performanceCommandQueueScopes({ type: 'headphone_calibrate' }), ['headphone']);
		assert.equal(ipc.performanceCommandQueueScopes({ type: 'headphone_calibrate_abort' }), null,
			'if abort queues behind the calibration it is meant to stop then it can never stop it - broken');
		const uninstall = ipc.installPerformanceBrowserIpc();
		try {
			await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'master_delay_ms', value: 1501 }), /master delay must be a finite number within 0\.\.1500/);
			await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'headphone_alignment_mode', value: 'serato' }), /alignment_mode must be/);
			await assert.rejects(ipc.dispatchPerformanceCommand({ type: 'headphone_calibrate', extra: 1 }), /unexpected fields/);
			await ipc.dispatchPerformanceCommand({ type: 'master_delay_ms', value: 123 });
			assert.equal(ipc.queryPerformanceState().mixer.headphones.master_delay_ms, 123);
			await ipc.dispatchPerformanceCommand({ type: 'headphone_alignment_mode', value: 'delay_all' });
			assert.equal(ipc.queryPerformanceState().mixer.headphones.alignment_mode, 'delay_all');
		} finally {
			uninstall();
		}
	});
});

describe('HeadphoneCluster / Mixer wiring (source guards; the components are rune-bound)', () => {
	test('the CALIBRATE button, the ROOM readout and the modal are wired', async () => {
		const [cluster, mixer, modal] = await Promise.all([
			readFile('src/lib/components/rb/mixer/HeadphoneCluster.svelte', 'utf8'),
			readFile('src/lib/components/rb/Mixer.svelte', 'utf8'),
			readFile('src/lib/components/rb/mixer/CueAlignModal.svelte', 'utf8')
		]);
		assert.match(cluster, /aria-label="CALIBRATE CUE ALIGNMENT"/);
		assert.match(cluster, /calibrateButtonEnabled\(/, 'if the button is not gated by the pure predicate then the External Headphones acceptance line is untested - broken');
		assert.match(cluster, /ROOM \+\{/, 'if the room delay is not shown then the operator cannot see the waveform is lagged on purpose - broken');
		assert.match(cluster, /data-performance-control="room-delay"/);
		assert.match(cluster, /oncalibrate/);
		assert.match(mixer, /CueAlignModal/);
		assert.match(mixer, /type: 'headphone_alignment_mode'/);
		assert.match(modal, /width: min\(720px, 96vw\)/);
		assert.match(modal, /Start calibration/);
		assert.match(modal, /playing decks are paused while it runs/i);
		assert.match(modal, /Hold one ear cup against the laptop microphone/);
		for (const mode of ['headphones_only', 'delay_all', 'hybrid']) {
			assert.match(modal, new RegExp(`value="${mode}"`), `mode radio ${mode} must exist`);
		}
	});
});
