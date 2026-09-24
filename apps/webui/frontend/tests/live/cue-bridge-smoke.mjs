/**
 * Silent headless proof that the cue AudioWorklet bridge wires, primes, preserves
 * stereo, and holds the knowable target-buffer delay.
 *
 * Wiring is imported from `wireCueBridgeNodes` in player/cue-bridge-wiring.ts (bundled
 * below) so this test exercises the same MessageChannel port-transfer path the
 * app uses. The processor module is served as `/processor.js` because
 * `addModule` needs a URL; the bundled `?url` import resolves to that path.
 *
 * Run (from apps/webui/frontend):
 *
 *   node tests/live/cue-bridge-smoke.mjs
 *
 * Regression lines:
 *  - if MessageChannel ports are structured-cloned into processorOptions then
 *    the bridge throws DataCloneError and cue output never connects
 *  - if the receiver never primes to the target fill then latency is whatever
 *    the startup race produces instead of CUE_BRIDGE_TARGET_FRAMES
 *  - if only the left channel is bridged then stereo cue monitor collapses
 *  - if bridge delay wanders by more than two render quanta then calibration
 *    cannot treat the buffer as a fixed offset
 *  - if underruns fire after priming then the ring is not holding its target
 */
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import { chromium } from '@playwright/test';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = join(FRONTEND_ROOT, 'src/lib');
const PROCESSOR_PATH = join(LIB_ROOT, 'player/cue-bridge-processor.js');
const PROCESSOR_SOURCE = readFileSync(PROCESSOR_PATH, 'utf8');
const SAMPLE_RATE = 48000;
const RENDER_QUANTUM = 128;
const TARGET_FRAMES = 2048;

async function bundleBridgeLogic() {
	const dir = mkdtempSync(join(tmpdir(), 'cue-bridge-smoke-'));
	const entry = join(dir, 'entry.ts');
	writeFileSync(
		entry,
		[
			"import { wireCueBridgeNodes } from '$lib/player/cue-bridge-wiring.ts'; import { CUE_BRIDGE_TARGET_FRAMES } from '$lib/player/headphones.ts';",
			'(globalThis as any).__bridge = { wireCueBridgeNodes, CUE_BRIDGE_TARGET_FRAMES };',
			''
		].join('\n')
	);
	const out = await build({
		entryPoints: [entry],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		define: {
			$state: 'globalThis.__identityState',
			'import.meta.env.VITE_API_BASE': 'undefined'
		},
		plugins: [
			{
				name: 'processor-url',
				setup(build) {
					build.onResolve({ filter: /\?url$/ }, (args) => ({
						path: args.path,
						namespace: 'processor-url'
					}));
					build.onLoad({ filter: /.*/, namespace: 'processor-url' }, () => ({
						contents: 'export default "/processor.js";',
						loader: 'js'
					}));
				}
			}
		],
		format: 'iife',
		logLevel: 'silent',
		platform: 'browser',
		target: 'es2022',
		write: false
	});
	return out.outputFiles[0].text;
}

const IN_PAGE = async ({ sampleRate, targetFrames, renderQuantum }) => {
	const { wireCueBridgeNodes } = globalThis.__bridge;
	const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
	// `page.evaluate` serializes only this function's OWN source text, not its
	// enclosing module scope, so a helper this function calls has to be
	// declared inside it - a module-level `estimateFrequencyHz` here would
	// compile in Node but throw `ReferenceError` the moment it actually ran in
	// the page.
	function estimateFrequencyHz(samples, rate) {
		let crossings = 0;
		for (let i = 1; i < samples.length; i += 1) {
			if ((samples[i - 1] <= 0 && samples[i] > 0) || (samples[i - 1] >= 0 && samples[i] < 0)) {
				crossings += 1;
			}
		}
		const seconds = samples.length / rate;
		return crossings / 2 / seconds;
	}

	const mainCtx = new AudioContext({ sampleRate });
	const cueCtx = new AudioContext({ sampleRate });
	await mainCtx.resume();
	await cueCtx.resume();

	let underruns = 0;
	const processorErrors = [];
	const { bridgeSender, bridgeReceiver } = await wireCueBridgeNodes(mainCtx, cueCtx, {
		onUnderrun: () => {
			underruns += 1;
		},
		onProcessorError: (side) => processorErrors.push(side)
	});

	const splitter = cueCtx.createChannelSplitter(2);
	const analyserL = cueCtx.createAnalyser();
	const analyserR = cueCtx.createAnalyser();
	analyserL.fftSize = 2048;
	analyserR.fftSize = 2048;
	bridgeReceiver.connect(splitter);
	splitter.connect(analyserL, 0);
	splitter.connect(analyserR, 1);
	const silent = cueCtx.createGain();
	silent.gain.value = 0;
	analyserL.connect(silent);
	analyserR.connect(silent);
	silent.connect(cueCtx.destination);

	const oscL = mainCtx.createOscillator();
	const oscR = mainCtx.createOscillator();
	oscL.frequency.value = 1000;
	oscR.frequency.value = 500;
	const merger = mainCtx.createChannelMerger(2);
	oscL.connect(merger, 0, 0);
	oscR.connect(merger, 0, 1);
	merger.connect(bridgeSender);
	oscL.start();
	oscR.start();

	const primingMs = (targetFrames / sampleRate) * 1000 + 250;
	await sleep(primingMs);

	const bufL = new Float32Array(analyserL.fftSize);
	const bufR = new Float32Array(analyserR.fftSize);
	analyserL.getFloatTimeDomainData(bufL);
	analyserR.getFloatTimeDomainData(bufR);
	const freqL = estimateFrequencyHz(bufL, sampleRate);
	const freqR = estimateFrequencyHz(bufR, sampleRate);

	// The impulse-delay measurement needs a quiet bridge: the oscillators are
	// still feeding a continuous tone through the exact splitter tap the delay
	// detector reads, so "first sample past threshold" would fire on that tone
	// within the first render quantum instead of on the impulse, resolving the
	// detector before the impulse (scheduled 50ms out) ever arrives. Stopping
	// them and letting the bridge's own target latency drain the tail keeps the
	// bridge itself (its worklets, its ring) exactly as exercised by priming;
	// only the oscillators, which have already done their job for the frequency
	// check above, go away.
	const expectedDrainMs = (targetFrames / sampleRate) * 1000 + 100;
	oscL.stop();
	oscR.stop();
	await sleep(expectedDrainMs);

	const measureImpulseDelayMs = async () => {
		// Cross-context wall-clock correlation via `getOutputTimestamp` was tried
		// first and rejected: in headless muted chromium there is no real device
		// behind either context, and measured live its `performanceTime` tracked
		// "now" to within about 1ms regardless of how far `contextTime` actually
		// lagged `currentTime`, which silently produced a large, consistent
		// negative "delay". Tapping the impulse on BOTH sides of the bridge with
		// `performance.now()` and diffing those two readings never has to trust
		// either context's own idea of wall-clock correspondence - it only needs
		// one shared clock, which `performance.now()` already is.
		let departPerf = null;
		let arrivePerf = null;

		const senderTap = mainCtx.createScriptProcessor(256, 2, 1);
		const senderMute = mainCtx.createGain();
		senderMute.gain.value = 0;
		const receiverTap = cueCtx.createScriptProcessor(256, 2, 1);
		const receiverMute = cueCtx.createGain();
		receiverMute.gain.value = 0;
		senderTap.connect(senderMute);
		senderMute.connect(mainCtx.destination);
		splitter.connect(receiverTap);
		receiverTap.connect(receiverMute);
		receiverMute.connect(cueCtx.destination);

		const impulse = mainCtx.createBuffer(2, 1, sampleRate);
		impulse.getChannelData(0)[0] = 1;
		impulse.getChannelData(1)[0] = 1;
		const impulseMerger = mainCtx.createChannelMerger(2);
		const src = mainCtx.createBufferSource();
		src.buffer = impulse;
		src.connect(impulseMerger, 0, 0);
		src.connect(impulseMerger, 0, 1);
		impulseMerger.connect(bridgeSender);
		impulseMerger.connect(senderTap);
		src.start(mainCtx.currentTime + 0.05);

		// A ScriptProcessorNode callback fires once its whole block is ready, at
		// roughly the block's END - so `performance.now()` at callback time is
		// biased late by however far into the block the crossing sample actually
		// sits (up to a full `bufferSize` of jitter, comfortably more than the
		// two-render-quantum tolerance this measurement is held to). Correcting
		// by the number of samples still remaining in the block after the
		// crossing index recovers the sample's own instant instead of the
		// block's.
		const crossingInstant = (event, crossIndex) => {
			const remaining = event.inputBuffer.length - crossIndex;
			return performance.now() - (remaining / event.inputBuffer.sampleRate) * 1000;
		};

		senderTap.onaudioprocess = (event) => {
			if (departPerf !== null) return;
			const input = event.inputBuffer.getChannelData(0);
			for (let i = 0; i < input.length; i += 1) {
				if (Math.abs(input[i]) > 0.05) {
					departPerf = crossingInstant(event, i);
					break;
				}
			}
		};

		await new Promise((resolve) => {
			const deadline = performance.now() + 800;
			receiverTap.onaudioprocess = (event) => {
				const input = event.inputBuffer.getChannelData(0);
				for (let i = 0; i < input.length; i += 1) {
					if (Math.abs(input[i]) > 0.05 && arrivePerf === null) {
						arrivePerf = crossingInstant(event, i);
						break;
					}
				}
				if (arrivePerf !== null || performance.now() >= deadline) resolve();
			};
		});

		senderTap.onaudioprocess = null;
		receiverTap.onaudioprocess = null;
		senderTap.disconnect();
		senderMute.disconnect();
		receiverTap.disconnect();
		receiverMute.disconnect();
		if (departPerf === null || arrivePerf === null) return Number.NaN;
		return arrivePerf - departPerf;
	};

	// A single pairwise `performance.now()` read at each ScriptProcessorNode
	// callback is quantized to that node's own 256-sample buffer boundary, and
	// the sender and receiver taps run on two independent AudioContexts whose
	// buffer boundaries are not phase-locked to each other, so one measurement
	// alone can read up to roughly two buffers (about 10.7ms) off the bridge's
	// actual, exactly-quantum-accurate latency - already proven at the frame
	// level by CUEOUT-18/19. Averaging several trials converges the MEAN back
	// onto the true value; the run-to-run SPREAD is bounded separately, wider
	// than the bridge's own two-quantum guarantee, because that spread is
	// measuring this JS-thread technique's resolution floor, not the bridge.
	const TRIAL_COUNT = 8;
	const delays = [];
	for (let i = 0; i < TRIAL_COUNT; i += 1) {
		delays.push(await measureImpulseDelayMs());
		await sleep(400);
	}
	await sleep(8000);

	await mainCtx.close();
	await cueCtx.close();

	const expectedMs = (targetFrames / sampleRate) * 1000;
	const toleranceMs = (renderQuantum * 2 / sampleRate) * 1000;
	const validDelays = delays.filter((d) => Number.isFinite(d));
	const meanDelayMs =
		validDelays.length > 0 ? validDelays.reduce((sum, d) => sum + d, 0) / validDelays.length : Number.NaN;
	const spreadMs = validDelays.length > 0 ? Math.max(...validDelays) - Math.min(...validDelays) : Number.NaN;

	return {
		freqL,
		freqR,
		expectedMs,
		toleranceMs,
		delays,
		meanDelayMs,
		spreadMs,
		underruns,
		processorErrors
	};
};

const bundle = await bundleBridgeLogic();
const server = createServer((request, response) => {
	if (request.url === '/processor.js') {
		response.writeHead(200, { 'content-type': 'application/javascript' });
		response.end(PROCESSOR_SOURCE);
		return;
	}
	response.writeHead(200, { 'content-type': 'text/html' });
	response.end('<!doctype html><title>cue bridge smoke</title>');
});

const browser = await chromium.launch({
	headless: true,
	args: ['--mute-audio', '--autoplay-policy=no-user-gesture-required']
});

try {
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	const port = server.address().port;
	const page = await browser.newPage();
	await page.goto(`http://127.0.0.1:${port}/`);
	await page.addScriptTag({ content: 'globalThis.__identityState = (v) => v;' });
	await page.addScriptTag({ content: bundle });
	const result = await page.evaluate(IN_PAGE, {
		sampleRate: SAMPLE_RATE,
		targetFrames: TARGET_FRAMES,
		renderQuantum: RENDER_QUANTUM
	});

	// The spread bound is wider than `toleranceMs` on purpose: it is bounding
	// this JS-thread ScriptProcessorNode measurement's own resolution floor
	// (see the comment above `TRIAL_COUNT`), not the bridge. The bridge's exact
	// per-quantum behaviour is already asserted, deterministically, by
	// CUEOUT-18/19; what this smoke test can additionally prove is that the
	// mean over several real trials lands on the expected target and does not
	// drift, in a real browser, through the real wiring path.
	const measurementJitterMs = result.toleranceMs * 4;

	console.log(`chromium=${browser.version()} sampleRate=${SAMPLE_RATE}`);
	console.log(`expected bridge delay ${result.expectedMs.toFixed(3)} ms +/- ${result.toleranceMs.toFixed(3)} ms`);
	console.log(`measured delays (ms): ${result.delays.map((d) => d.toFixed(2)).join(', ')}`);
	console.log(
		`mean delay=${result.meanDelayMs.toFixed(3)} ms spread=${result.spreadMs.toFixed(3)} ms (jitter bound ${measurementJitterMs.toFixed(3)} ms)`
	);
	console.log(`freq left=${result.freqL.toFixed(1)} Hz right=${result.freqR.toFixed(1)} Hz`);
	console.log(`underruns after priming=${result.underruns}`);

	const freqOk = Math.abs(result.freqL - 1000) < 120 && Math.abs(result.freqR - 500) < 80;
	const delayOk =
		result.delays.every((d) => Number.isFinite(d)) &&
		Math.abs(result.meanDelayMs - result.expectedMs) <= result.toleranceMs &&
		result.spreadMs <= measurementJitterMs;
	const underrunOk = result.underruns === 0;

	if (!freqOk) {
		console.error('[FAIL] stereo frequency detection out of range');
		process.exit(1);
	}
	if (!delayOk) {
		console.error('[FAIL] bridge delay outside tolerance, a trial failed to detect the impulse, or spread exceeded the measurement jitter bound');
		process.exit(1);
	}
	if (!underrunOk) {
		console.error('[FAIL] underruns reported after priming');
		process.exit(1);
	}
	if (result.processorErrors.length > 0) {
		console.error(`[FAIL] bridge worklet threw: ${result.processorErrors.join(', ')}`);
		process.exit(1);
	}
	console.log('ok cue bridge smoke');
	process.exit(0);
} finally {
	await browser.close();
	server.close();
}
