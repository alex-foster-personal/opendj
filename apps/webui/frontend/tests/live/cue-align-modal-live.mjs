/**
 * Drive the REAL cue alignment modal, in the real app, on real devices.
 *
 * `cue-align-two-outputs.mjs` proves the measurement. This proves the
 * DELIVERY: the page a person actually clicks, with its device pickers, its
 * live level bar, its Continue button and its Run again button, against the
 * real speakers, the real headphone jack and the real microphone.
 *
 * It drives the page through `window.musicDjToolsPerformance`, the same IPC
 * surface an agent gets, and reads the modal back out of the DOM, so a step
 * that renders wrong fails here even when the state machine is right.
 *
 * Needs the app served from this worktree on its own ports (never :9448):
 *
 *   .venv/bin/python3 -m apps.engine_core serve --data-dir <dir> --port 8761
 *   apps/webui/frontend: MUSIC_DJ_BACKEND_PORT=8761 vite dev --port 9461 --strictPort
 *   node tests/live/cue-align-modal-live.mjs --url http://localhost:9461/performance
 *
 * Regression lines:
 *  - if CALIBRATE is not enabled after a cue output is selected then the modal
 *    is unreachable for the device the feature exists to align
 *  - if the level bar never renders during a ramp then the operator has no way
 *    to tell a cup that is close enough from one that is not
 *  - if Run again is still disabled after a failed run then a first failure
 *    ends the session at the modal
 */
import { chromium } from '@playwright/test';

function arg(name, fallback = null) {
	const i = process.argv.indexOf(`--${name}`);
	return i === -1 ? fallback : (process.argv[i + 1] ?? true);
}
const URL_ = String(arg('url', 'http://localhost:9461/performance'));
const CUE_MATCH = String(arg('cue', 'External Headphones'));
const MIC_MATCH = String(arg('mic', 'Brio'));
const MODE = String(arg('mode', 'hybrid'));
const HOLD = Number(arg('hold', 0));
const DIAG = process.argv.includes('--diag');

const results = [];
const check = (name, ok, detail = '') => {
	results.push({ name, ok, detail });
	console.log(`${ok ? '[OK]  ' : '[FAIL]'} ${name}${detail === '' ? '' : `  ${detail}`}`);
};

const browser = await chromium.launch({
	headless: false,
	// This Mac routinely sits at 20x core count, where a browser takes minutes
	// rather than seconds to come up. A launch timeout is not a result.
	timeout: 600_000,
	args: ['--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream']
});
try {
	const page = await browser.newPage();
	const consoleErrors = [];
	page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
	if (DIAG) {
		// Tap every mic source the page creates: which context, what the track
		// reports, and the level actually arriving in THAT context.
		await page.addInitScript(() => {
			window.__diag = [];
			window.__timing = [];
			window.__sinks = [];
			const origSink = AudioContext.prototype.setSinkId;
			if (typeof origSink === 'function') {
				AudioContext.prototype.setSinkId = function (id) {
					const entry = { requested: typeof id === 'string' ? id.slice(0, 12) : JSON.stringify(id), rate: this.sampleRate };
					window.__sinks.push(entry);
					return origSink.call(this, id).then(
						(v) => { entry.result = 'ok'; entry.sinkId = String(this.sinkId).slice(0, 12); return v; },
						(e) => { entry.result = String(e); throw e; }
					);
				};
			}
			const origSP = AudioContext.prototype.createScriptProcessor;
			AudioContext.prototype.createScriptProcessor = function (...a) {
				const node = origSP.apply(this, a);
				const ctx = this;
				let first = true;
				const desc = Object.getOwnPropertyDescriptor(ScriptProcessorNode.prototype, 'onaudioprocess');
				Object.defineProperty(node, 'onaudioprocess', {
					configurable: true,
					get() { return desc.get.call(node); },
					set(fn) {
						desc.set.call(node, fn === null ? null : (ev) => {
							if (first) {
								first = false;
								window.__timing.push({
									kind: 'capture-open', currentTime: ctx.currentTime, playbackTime: ev.playbackTime,
									bufferSec: node.bufferSize / ctx.sampleRate, baseLatency: ctx.baseLatency,
									outputLatency: ctx.outputLatency
								});
							}
							return fn(ev);
						});
					}
				});
				return node;
			};
			const origStart = AudioBufferSourceNode.prototype.start;
			AudioBufferSourceNode.prototype.start = function (when, ...rest) {
				window.__timing.push({ kind: 'chirp-start', when, currentTime: this.context.currentTime, durSec: this.buffer?.duration });
				return origStart.call(this, when, ...rest);
			};
			const orig = AudioContext.prototype.createMediaStreamSource;
			AudioContext.prototype.createMediaStreamSource = function (stream) {
				const node = orig.call(this, stream);
				const track = stream.getAudioTracks()[0];
				const an = this.createAnalyser();
				an.fftSize = 2048;
				node.connect(an);
				const buf = new Float32Array(an.fftSize);
				const entry = {
					ctxState: this.state, ctxRate: this.sampleRate, sinkId: String(this.sinkId ?? ''),
					label: track?.label, muted: track?.muted, enabled: track?.enabled,
					readyState: track?.readyState, settings: track?.getSettings(), peaks: []
				};
				window.__diag.push(entry);
				const id = setInterval(() => {
					an.getFloatTimeDomainData(buf);
					let peak = 0;
					for (const v of buf) peak = Math.max(peak, Math.abs(v));
					entry.peaks.push(Number(peak.toFixed(4)));
					entry.ctxState = this.state;
					entry.muted = track?.muted;
					entry.readyState = track?.readyState;
					if (entry.peaks.length > 60) clearInterval(id);
				}, 250);
				return node;
			};
		});
	}
	await page.goto(URL_, { waitUntil: 'domcontentloaded' });
	await page.waitForFunction(() => window.musicDjToolsPerformance !== undefined, { timeout: 60_000 });
	check('the performance page installed its IPC bridge', true);

	// A gesture, then the device pickers. Both go through the IPC surface an
	// agent would use, which is also what proves the parity claim.
	await page.mouse.click(5, 5);
	const devices = await page.evaluate(async ({ cueMatch, micMatch, mode }) => {
		const ipc = window.musicDjToolsPerformance;
		await ipc.dispatch({ type: 'headphone_outputs_refresh' });
		const hp = ipc.query().mixer.headphones;
		// Prefer a REAL device over the "Default - <name>" alias. The alias has
		// deviceId "default" and follows the OS default output, so picking it as
		// the cue device silently points cue and master at the same hardware the
		// moment that device also happens to be the system default: the run then
		// measures one device twice and blames the microphone.
		const find = (list, needle) => {
			const hits = list.filter((d) => d.label.toLowerCase().includes(needle.toLowerCase()));
			return hits.find((d) => d.id !== 'default' && !/^default - /i.test(d.label)) ?? hits[0] ?? null;
		};
		const cue = find(hp.outputs, cueMatch);
		const mic = find(hp.inputs, micMatch);
		if (cue === null || mic === null) {
			return { error: `no match`, outputs: hp.outputs, inputs: hp.inputs };
		}
		await ipc.dispatch({ type: 'headphone_output_select', device_id: cue.id });
		await ipc.dispatch({ type: 'headphone_input_select', device_id: mic.id });
		await ipc.dispatch({ type: 'headphone_alignment_mode', value: mode });
		const after = ipc.query().mixer.headphones;
		return {
			cue: cue.label,
			mic: mic.label,
			output_mode: after.output_mode,
			selected: after.selected_output_device_id,
			alignment_mode: after.alignment_mode
		};
	}, { cueMatch: CUE_MATCH, micMatch: MIC_MATCH, mode: MODE });

	if (devices.error !== undefined) {
		console.log('outputs:', devices.outputs?.map((d) => d.label));
		console.log('inputs: ', devices.inputs?.map((d) => d.label));
		throw new Error(`could not select devices: ${devices.error}`);
	}
	check(
		'cue output selected, and selecting it put the monitor in two_outputs',
		devices.output_mode === 'two_outputs',
		`cue="${devices.cue}" mic="${devices.mic}" mode=${devices.output_mode}`
	);

	// A fresh engine data dir shows the first-run setup overlay over the whole
	// page. Minimise it the way a person would; it keeps its step.
	const setup = page.locator('[aria-label="First-run setup"]');
	if (await setup.isVisible()) {
		await setup.locator('button.su-min').click();
		await setup.waitFor({ state: 'hidden', timeout: 15_000 });
		check('the first-run setup overlay was minimised out of the way', true);
	}

	const calibrate = page.locator('[aria-label="CALIBRATE CUE ALIGNMENT"]');
	await calibrate.waitFor({ state: 'visible', timeout: 15_000 });
	check('CALIBRATE is enabled with a cue device selected', await calibrate.isEnabled());
	await calibrate.click();

	const panel = page.locator('[aria-label="Cue alignment calibration"]');
	await panel.waitFor({ state: 'visible', timeout: 10_000 });
	check('the modal opened', true);

	const start = panel.getByRole('button', { name: /Start calibration|Run again/ });
	check('Start calibration is clickable before a run', await start.isEnabled());
	if (DIAG) {
		// Vite serves the session module by URL, so this import is the SAME
		// instance the modal uses: record every probe the controller publishes.
		await page.evaluate(async () => {
			// After a hot edit Vite stamps importers with ?t=, so ask the modal
			// which URL it actually imported rather than guessing.
			const modalSrc = await (await fetch('/src/lib/components/rb/mixer/CueAlignModal.svelte')).text();
			const url = modalSrc.match(/"(\/src\/lib\/rb\/cue-align-session\.svelte\.ts[^"]*)"/)[1];
			// The probe lives in the shared read model, so read it where every
			// agent reads it: the session module's own mixerState import.
			const sessionSrc = await (await fetch(url)).text();
			const stateUrl = sessionSrc.match(/"(\/src\/lib\/player\/state\.svelte\.ts[^"]*)"/)[1];
			const state = await import(stateUrl);
			window.__probes = [];
			let last = '';
			setInterval(() => {
				const p = state.mixerState.headphones.calibration.probe;
				if (p === null || p.peak === null) return;
				const key = JSON.stringify(p);
				if (key !== last) { last = key; window.__probes.push({ ...p, t: performance.now() }); }
			}, 20);
		});
	}
	const startedAt = Date.now();
	await start.click();

	// Watch the ramp. The level bar is the delivery claim: it must actually
	// appear and actually move while stage one climbs.
	const levelReads = new Set();
	let sawContinue = false;
	const deadline = Date.now() + 180_000;
	let step = 'idle';
	while (Date.now() < deadline) {
		if (await panel.count() === 0) { step = 'modal-gone'; break; }
		step = (await panel.getAttribute('data-cue-align-step')) ?? 'idle';
		const level = panel.locator('.ca-level-read');
		if (await level.count() > 0) {
			const text = (await level.first().textContent())?.trim();
			if (text !== undefined && text !== '') levelReads.add(text);
		}
		if (step === 'mic_check_cue' && !sawContinue) {
			const cont = panel.getByRole('button', { name: 'Continue' });
			if (await cont.count() > 0 && await cont.isEnabled()) {
				if (HOLD > 0) {
					console.log(`\n>>> HOLD AN EAR CUP AGAINST THE MICROPHONE. Continuing in ${HOLD}s.\n`);
					await page.waitForTimeout(HOLD * 1000);
				}
				await cont.click();
				sawContinue = true;
				check('the ear-cup prompt offered Continue, and it fired', true);
			}
		}
		if (step === 'applied' || step === 'failed') break;
		await page.waitForTimeout(250);
	}
	const seconds = ((Date.now() - startedAt) / 1000).toFixed(1);

	check('the live level readout rendered during the ramp', levelReads.size > 0,
		`${levelReads.size} distinct readings`);
	for (const read of [...levelReads].slice(0, 12)) console.log(`        ${read}`);

	const body = (await panel.textContent()) ?? '';
	console.log(`\nfinal step: ${step}  (${seconds}s)`);
	if (DIAG) {
		for (const p of await page.evaluate(() => window.__probes)) {
			console.log(`probe ${p.bus.padEnd(6)} gain ${p.gain.toFixed(2)} peak ${p.peak.toFixed(2)} lag ${p.lagMs} ms`);
		}
		console.log(await panel.locator('.ca-error, [role="alert"], p').first().textContent().catch(() => ''));
	}
	if (DIAG) {
		console.log('sinks:', JSON.stringify(await page.evaluate(() => window.__sinks)));
		console.log('cue id:', JSON.stringify(devices.selected).slice(0, 14));
	}
	if (DIAG) console.log('timing:', JSON.stringify(await page.evaluate(() => window.__timing)));
	const summary = body.replace(/\s+/g, ' ').match(/(Applied\.[^]*?ms\.)|(The mic could not hear[^]*?\.)/);
	if (summary !== null) console.log(`modal says: ${summary[0].trim()}`);

	await page.screenshot({ path: 'cue-align-modal-live.png' });
	console.log('screenshot: apps/webui/frontend/cue-align-modal-live.png');

	// The Run again defect: after a run ENDS, the button must be clickable.
	const again = panel.getByRole('button', { name: /Run again|Start calibration/ });
	await page.waitForTimeout(500);
	check('Run again is clickable after the run ended', await again.isEnabled(),
		'this is the binding that read a non-reactive closure variable');

	check('no console errors from the page', consoleErrors.length === 0,
		consoleErrors.slice(0, 2).join(' | '));
	check('the run reached a terminal step', step === 'applied' || step === 'failed', step);

	const failed = results.filter((r) => !r.ok);
	console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
	process.exit(failed.length === 0 && step === 'applied' ? 0 : 1);
} finally {
	await browser.close();
}
