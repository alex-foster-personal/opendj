/**
 * Does the shipped cue alignment calibration work on real hardware, driving
 * two real output devices from one AudioContext, with a real microphone?
 *
 * This is the harness that answers "prove it, do not reason about it". It runs
 * the SHIPPED decision path -- `createCueAlignController`, the gain ladder, the
 * cross-correlation, the 0.35 peak threshold, the median-of-three and the
 * spread refusal -- inside a real Chromium against the real sound devices on
 * this Mac. Only `cueAlignAudioEffects` is restated here (30 lines, mirroring
 * player/headphones.ts) because the shipped one is bound to the live mixer
 * store, which needs the whole app booted.
 *
 * Why a browser at all: an ffmpeg rig cannot measure the OFFSET, only
 * detectability. Two `ffmpeg` launches are two processes with their own
 * variable device-open latency, so the difference between their two lags is
 * that jitter, not the buses. The app has ONE AudioContext feeding both
 * outputs, which is the entire reason its offset means anything, so a harness
 * that wants to prove the app must use the app's clock.
 *
 * Run (from apps/webui/frontend):
 *
 *   node tests/live/cue-align-two-outputs.mjs --cue "External Headphones" --mic Brio
 *   node tests/live/cue-align-two-outputs.mjs --list        # what this Mac has
 *   node tests/live/cue-align-two-outputs.mjs --repeat 3    # stability
 *
 * MASTER is always the OS default output, exactly as `ctx.destination` is in
 * the app. Set that in System Settings before running.
 *
 * Regression lines:
 *  - if the cue leg never clears 0.35 at any rung then headphone calibration
 *    cannot work on this hardware and the feature is measuring nothing
 *  - if the gain ladder stops on rung one for a bus a human cannot hear then
 *    the threshold is too generous and a noise correlation is passing
 *  - if repeated runs disagree by more than CUE_ALIGN_MAX_SPREAD_MS then the
 *    offset drifts faster than a set lasts, and calibrating once is not enough
 */
import { createServer } from 'node:http';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import { chromium } from '@playwright/test';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = join(FRONTEND_ROOT, 'src/lib');

function arg(name, fallback = null) {
	const i = process.argv.indexOf(`--${name}`);
	return i === -1 ? fallback : (process.argv[i + 1] ?? true);
}
const LIST_ONLY = process.argv.includes('--list');
const CUE_MATCH = String(arg('cue', 'External Headphones'));
const MIC_MATCH = String(arg('mic', 'Brio'));
const REPEAT = Number(arg('repeat', 1));
const MODE = String(arg('mode', 'hybrid'));
const EXTRA_PREROLL = Number(arg('extra-preroll', 0));
// Diagnostic: a constant -60 dBFS noise bed on the cue bus, to test whether the
// <audio> sink re-times its buffer across silence.
const CUE_BED = process.argv.includes('--cue-bed');

/** The shipped modules, bundled for the page. Nothing is reimplemented. */
async function bundleShippedLogic() {
	const dir = mkdtempSync(join(tmpdir(), 'cue-align-live-'));
	const entry = join(dir, 'entry.ts');
	writeFileSync(
		entry,
		[
			"import * as align from '$lib/player/cue-align.svelte';",
			"import * as latency from '$lib/player/cue-latency';",
			'(globalThis as any).__shipped = { align, latency };',
			''
		].join('\n')
	);
	const out = await build({
		entryPoints: [entry],
		absWorkingDir: FRONTEND_ROOT,
		alias: { $lib: LIB_ROOT },
		bundle: true,
		// cue-align.svelte.ts holds no runes (its flags are plain closure
		// variables); the define is here so a future rune cannot silently
		// bundle into a ReferenceError at run time.
		define: { $state: 'globalThis.__identityState' },
		format: 'iife',
		logLevel: 'silent',
		platform: 'browser',
		target: 'es2022',
		write: false
	});
	return out.outputFiles[0].text;
}

/**
 * The audio half, mirroring `cueAlignAudioEffects` in player/headphones.ts:
 * master is `ctx.destination` (the OS default output), cue is a
 * MediaStreamAudioDestinationNode played through an <audio> element pinned to
 * the chosen device with setSinkId, and the capture is a ScriptProcessor on
 * its own AudioContext. Runs in the page.
 */
const IN_PAGE = async ({ cueMatch, micMatch, mode, repeat, extraPreroll, cueBed }) => {
	const shipped = globalThis.__shipped;
	const log = [];
	const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
	for (const track of stream.getTracks()) track.stop();
	const devices = await navigator.mediaDevices.enumerateDevices();
	const pick = (kind, needle) =>
		devices.find((d) => d.kind === kind && d.label.toLowerCase().includes(needle.toLowerCase())) ?? null;
	const cueDevice = pick('audiooutput', cueMatch);
	const micDevice = pick('audioinput', micMatch);
	const inventory = devices.map((d) => ({ kind: d.kind, label: d.label, id: d.deviceId }));
	if (cueDevice === null || micDevice === null) {
		return { inventory, error: `no ${cueDevice === null ? 'output' : 'input'} device matched` };
	}

	const ctx = new AudioContext();
	await ctx.resume();
	const cueDest = ctx.createMediaStreamDestination();
	const cueEl = new Audio();
	cueEl.srcObject = cueDest.stream;
	await cueEl.setSinkId(cueDevice.deviceId);
	await cueEl.play();
	if (cueBed) {
		const bed = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
		const data = bed.getChannelData(0);
		for (let i = 0; i < data.length; i += 1) data[i] = (Math.random() * 2 - 1) * 0.001;
		const bedSrc = ctx.createBufferSource();
		bedSrc.buffer = bed;
		bedSrc.loop = true;
		bedSrc.connect(cueDest);
		bedSrc.start();
	}

	const effects = {
		sampleRate: () => ctx.sampleRate,
		async getUserMedia() {
			// The same three constraints `unlockAudioInputConstraints` sets in the
			// app, and they are load-bearing rather than tidy: echo cancellation
			// exists to SUBTRACT this machine's own loudspeaker output from the
			// mic, which is the exact signal a room measurement is trying to
			// hear. Left at its default the master leg reads pure noise.
			const mic = await navigator.mediaDevices.getUserMedia({
				audio: {
					deviceId: { exact: micDevice.deviceId },
					echoCancellation: false,
					noiseSuppression: false,
					autoGainControl: false
				},
				video: false
			});
			return { stream: mic, stop: () => mic.getTracks().forEach((t) => t.stop()) };
		},
		contextTimeSec: () => ctx.currentTime,
		async playTrain(bus, reference, sampleRate, signal, gain, whenSec) {
			signal.throwIfAborted();
			const buffer = ctx.createBuffer(1, reference.length, sampleRate);
			buffer.copyToChannel(reference, 0);
			const src = ctx.createBufferSource();
			src.buffer = buffer;
			const level = ctx.createGain();
			level.gain.value = gain;
			src.connect(level);
			level.connect(bus === 'master' ? ctx.destination : cueDest);
			try {
				await new Promise((resolve, reject) => {
					signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
					src.onended = resolve;
					src.start(whenSec);
				});
			} finally {
				src.disconnect();
				level.disconnect();
			}
		},
		// Mirrors `_openChirpCapture`: same context as the chirp, and it resolves
		// as soon as the mic is really delivering, reporting that instant.
		openCapture(handle, durationMs, sampleRate, signal) {
			const frames = Math.ceil((durationMs / 1000) * ctx.sampleRate);
			const out = new Float32Array(frames);
			let offset = 0;
			const src = ctx.createMediaStreamSource(handle.stream);
			const processor = ctx.createScriptProcessor(2048, 1, 1);
			const silent = ctx.createGain();
			silent.gain.value = 0;
			src.connect(processor);
			processor.connect(silent);
			silent.connect(ctx.destination);
			const teardown = () => {
				processor.onaudioprocess = null;
				processor.disconnect();
				src.disconnect();
				silent.disconnect();
			};
			return new Promise((resolveOpen, rejectOpen) => {
				let opened = false;
				const samples = new Promise((resolve, reject) => {
					const timer = setTimeout(() => {
						teardown();
						const error = new Error('record timed out');
						if (!opened) { opened = true; rejectOpen(error); }
						reject(error);
					}, durationMs + 2500);
					signal.addEventListener('abort', () => {
						clearTimeout(timer);
						teardown();
						const error = new Error('aborted');
						if (!opened) { opened = true; rejectOpen(error); }
						reject(error);
					}, { once: true });
					processor.onaudioprocess = (event) => {
						if (!opened) { opened = true; resolveOpen({ startedAtSec: ctx.currentTime, samples }); }
						const input = event.inputBuffer.getChannelData(0);
						const n = Math.min(input.length, frames - offset);
						out.set(input.subarray(0, n), offset);
						offset += n;
						if (offset >= frames) {
							clearTimeout(timer);
							teardown();
							resolve(out);
						}
					};
				});
				samples.catch(() => undefined);
			});
		},
		// `extraPreroll` is a diagnostic, not a feature: it delays the chirp past
		// the shipped 40 ms preroll so a run can tell "the mic could not hear it"
		// apart from "the capture had not started yet".
		sleep: (ms) => new Promise((r) => setTimeout(r, ms + extraPreroll)),
		async pauseDecks() { return []; },
		async resumeDecks() {},
		now: () => Date.now(),
		persist(result) { log.push({ persisted: result }); },
		alignmentMode: () => mode,
		deviceIds: () => ({ cue: cueDevice.deviceId, master: null }),
		onProbe(probe) { log.push({ probe }); }
	};

	const runs = [];
	for (let i = 0; i < repeat; i += 1) {
		const calibration = { step: 'idle', cue_latency_ms: null, master_latency_ms: null, offset_ms: null, error: null };
		const controller = shipped.align.createCueAlignController(effects, calibration);
		const startedAt = performance.now();
		await controller.run({ interactive: false });
		runs.push({ ...calibration, seconds: Number(((performance.now() - startedAt) / 1000).toFixed(1)) });
	}
	await ctx.close();
	return {
		inventory,
		sampleRate: ctx.sampleRate,
		cue: cueDevice.label,
		mic: micDevice.label,
		threshold: shipped.latency.CUE_LATENCY_PEAK_MIN,
		ladder: [...shipped.latency.CUE_LATENCY_GAIN_STEPS],
		runs,
		log
	};
};

const bundle = await bundleShippedLogic();
const server = createServer((_request, response) => {
	response.writeHead(200, { 'content-type': 'text/html' });
	response.end('<!doctype html><title>cue align live</title>');
});
const browser = await chromium.launch({
	headless: false,
	args: [
		'--autoplay-policy=no-user-gesture-required',
		// Grants the mic without a prompt. The DEVICE is real: this is the
		// fake UI, not the fake device.
		'--use-fake-ui-for-media-stream'
	]
});
try {
	const page = await browser.newPage();
	// `navigator.mediaDevices` exists only in a secure context, and about:blank
	// is not one. A loopback origin is, so the page is served from one.
	await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
	await page.goto(`http://127.0.0.1:${server.address().port}/`);
	await page.addScriptTag({ content: 'globalThis.__identityState = (v) => v;' });
	await page.addScriptTag({ content: bundle });
	const result = await page.evaluate(IN_PAGE, {
		cueMatch: CUE_MATCH,
		micMatch: MIC_MATCH,
		mode: MODE,
		repeat: LIST_ONLY ? 0 : REPEAT,
		extraPreroll: EXTRA_PREROLL,
		cueBed: CUE_BED
	});
	if (LIST_ONLY || result.error !== undefined) {
		if (result.error !== undefined) console.log(`[ERROR] ${result.error}`);
		for (const d of result.inventory) console.log(`${d.kind.padEnd(12)} ${d.label}`);
		process.exit(result.error === undefined ? 0 : 1);
	}
	console.log(`chromium=${browser.version()} sampleRate=${result.sampleRate}`);
	console.log(`master=OS default   cue="${result.cue}"   mic="${result.mic}"`);
	console.log(`threshold=${result.threshold}  ladder=${result.ladder.join(', ')}  extraPreroll=${EXTRA_PREROLL}ms\n`);
	for (const entry of result.log) {
		if (entry.probe === undefined || entry.probe.peak === null) continue;
		const p = entry.probe;
		console.log(
			`  ramp ${p.bus.padEnd(6)} gain ${String(p.gain).padEnd(5)} peak ${p.peak.toFixed(3)} ` +
				`lag ${String(p.lagMs).padStart(5)} ms ${p.peak >= p.threshold ? '<- heard' : ''}`
		);
	}
	console.log('');
	for (const [i, run] of result.runs.entries()) {
		console.log(
			`run ${i + 1}: ${run.step}  master ${run.master_latency_ms} ms  cue ${run.cue_latency_ms} ms  ` +
				`offset ${run.offset_ms} ms  (${run.seconds}s)${run.error === null ? '' : `\n        ${run.error}`}`
		);
	}
	const applied = result.runs.filter((r) => r.step === 'applied');
	if (applied.length > 1) {
		const offsets = applied.map((r) => r.offset_ms);
		console.log(`\noffset spread across ${applied.length} runs: ${Math.max(...offsets) - Math.min(...offsets)} ms`);
	}
	process.exit(applied.length === result.runs.length ? 0 : 1);
} finally {
	await browser.close();
	server.close();
}
