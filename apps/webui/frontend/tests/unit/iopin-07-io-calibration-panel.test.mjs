// requirement: IOPIN-07 / issue #11315 (I/O panel calibration placement and probes)
import assert from 'node:assert/strict';
import { after, before, describe, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const ENTRY = [
	"export { default as Panel } from '$lib/components/rb/mixer/HeadphoneCluster.svelte';",
	"export { mixerState } from '$lib/player/state.svelte';",
	"export { ioSurface, openIoView, closeIoView } from '$lib/rb/io-surface.svelte';",
	"export { cueAlignModal } from '$lib/rb/cue-align-session.svelte';",
	"export { render } from 'svelte/server';"
].join('\n');

/** SSR hydration markers are not markup. */
const stripMarkers = (html) => html.replace(/<!--[\s\S]*?-->/g, '');

let ssr;

before(async () => {
	installWindow('');
	globalThis.AudioContext = FakeAudioContext;
	if (typeof globalThis.CSS === 'undefined') {
		globalThis.CSS = { supports: () => false };
	}
	ssr = await loadSvelteSsrModule(ENTRY);
});

after(() => {
	delete globalThis.AudioContext;
	delete globalThis.window;
	delete globalThis.CSS;
});

const noop = () => {};
const noopPick = () => noop;
async function noopTestRun(work) {
	return work(() => true, new AbortController().signal);
}

function headphoneProps() {
	return {
		state: ssr.mixerState.headphones,
		onmix: noop,
		onlevel: noop,
		onboothlevel: noop,
		ondelay: noop,
		onrefresh: noop,
		onacquire: noop,
		onselect: noopPick,
		onmaster: noopPick,
		onbooth: noopPick,
		oninput: noopPick,
		onmode: noop,
		oncalibrate: noop,
		onAlignmentMode: noop,
		onprobe: noop,
		testrun: noopTestRun
	};
}

function wireTwoOutputsHeadphones() {
	const hp = ssr.mixerState.headphones;
	hp.output_mode = 'two_outputs';
	hp.supported = true;
	hp.outputs = [
		{ id: 'main', label: 'Built-in Output' },
		{ id: 'cue', label: 'Headphones' }
	];
	hp.inputs = [{ id: 'mic', label: 'Built-in Mic' }];
	hp.selected_master_output_device_id = 'main';
	hp.selected_output_device_id = 'cue';
	hp.selected_input_device_id = 'mic';
	hp.device_access = {
		status: 'listed',
		action: 'none',
		message: null,
		detail: null,
		output_pinning: true,
		notices: []
	};
	hp.signals.master = {
		state: 'idle',
		rms: 0.05,
		peak: 0.1,
		measured_at: null,
		source: 'application_bus',
		physical_output_proven: false
	};
	hp.signals.cue = { ...hp.signals.master };
	hp.signals.input = {
		state: 'inactive',
		rms: null,
		peak: null,
		measured_at: null,
		source: 'captured_input',
		physical_output_proven: false
	};
}

function renderHeadphoneCluster({ openPanel = true, showAlignModal = false } = {}) {
	if (openPanel) {
		ssr.openIoView();
	} else {
		ssr.closeIoView();
	}
	wireTwoOutputsHeadphones();
	ssr.cueAlignModal.open = showAlignModal;
	return stripMarkers(ssr.render(ssr.Panel, { props: headphoneProps() }).body);
}

describe('IOPIN-07 I/O calibration panel contracts', () => {
	test('[if] quiet-room unstable spread [then] error cites path latency, not room noise, [else stop].', async () => {
		const unstable = await loadTypeScriptModule('src/lib/player/cue-align-unstable.ts');
		const diagnosis = unstable.diagnoseUnstableMeasurement(
			[
				{ lagMs: 200, peak: 0.62, clippedFraction: 0 },
				{ lagMs: 205, peak: 0.58, clippedFraction: 0 },
				{ lagMs: 215, peak: 0.55, clippedFraction: 0 }
			],
			[
				{ lagMs: 900, peak: 0.61, clippedFraction: 0 },
				{ lagMs: 915, peak: 0.57, clippedFraction: 0 },
				{ lagMs: 905, peak: 0.56, clippedFraction: 0 }
			],
			{ comfortablePeak: 0.5, maxSpreadMs: 10 }
		);
		assert.match(diagnosis.message, /latency itself changed|output device/);
		assert.doesNotMatch(diagnosis.message, /less room noise|room noise is the cause/i);
	});

	test('[if] I/O panel open in two_outputs [then] calibrate block sits under device pickers, [else stop].', () => {
		const html = renderHeadphoneCluster();
		assert.match(html, /data-io-calibration-block/);
		assert.match(html, /aria-label="calibration stimulus"/);
		assert.match(html, /aria-label="CALIBRATE CUE ALIGNMENT"/);
		assert.match(html, /aria-label="master output device"/);
		const calIdx = html.indexOf('data-io-calibration-block');
		const masterIdx = html.indexOf('aria-label="master output device"');
		assert.ok(calIdx > masterIdx, 'calibration block follows the output pickers');
	});

	test('[if] I/O panel open [then] output and input rows expose role=meter activity bars, [else stop].', () => {
		const html = renderHeadphoneCluster();
		for (const bus of ['master', 'cue', 'input']) {
			assert.match(html, new RegExp(`data-signal-bus="${bus}"`));
		}
		assert.match(html, /role="meter"/);
	});

	test('[if] routing warn in two_outputs [then] feedback stays inside the open panel only, [else stop].', () => {
		const openHtml = renderHeadphoneCluster({ openPanel: true });
		assert.match(openHtml, /data-io-routing-feedback/);
		assert.match(openHtml, /data-audio-io-panel/);
		const closedHtml = renderHeadphoneCluster({ openPanel: false });
		assert.doesNotMatch(closedHtml, /data-io-routing-feedback/);
		assert.doesNotMatch(closedHtml, /data-io-calibration-block/);
	});

	test('[if] alignment modal is open [then] it embeds inside the calibration block, [else stop].', () => {
		const html = renderHeadphoneCluster({ showAlignModal: true });
		assert.match(html, /class="ca-overlay[^"]*\bembedded\b/);
		const calIdx = html.indexOf('data-io-calibration-block');
		const modalIdx = html.search(/class="ca-overlay[^"]*\bembedded\b/);
		assert.ok(calIdx !== -1 && modalIdx !== -1 && calIdx < modalIdx);
	});

	test('[if] mixer config persists probe choice [then] chirp and click_train both build references, [else stop].', async () => {
		const store = new Map();
		globalThis.window = {
			localStorage: {
				getItem: (key) => (store.has(key) ? store.get(key) : null),
				setItem: (key, value) => store.set(key, String(value)),
				removeItem: (key) => store.delete(key)
			}
		};
		const cueLatency = await loadTypeScriptModule('src/lib/player/cue-latency.ts');
		const mixerConfig = await loadTypeScriptModule('src/lib/player/mixer-config.ts');
		assert.equal(mixerConfig.loadMixerConfig().calibration_probe, 'chirp');
		mixerConfig.persistMixerConfig({ calibration_probe: 'click_train' });
		assert.equal(mixerConfig.loadMixerConfig().calibration_probe, 'click_train');
		const sweep = cueLatency.buildCalibrationReference(44100, 'chirp');
		const train = cueLatency.buildCalibrationReference(44100, 'click_train');
		assert.ok(sweep.length > train.length / 4);
		assert.ok(train.length > 100);
		mixerConfig.persistMixerConfig({ calibration_probe: 'chirp' });
	});
});
