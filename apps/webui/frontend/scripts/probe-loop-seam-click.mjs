// Probe: how loud is the loop seam in the stretch worklet today, would a pre-start
// crossfade fix it, and can the worklet play in reverse? (spike, issue #9843)
//
// Why: rolls and censor ((private path)) both
// depend on what the vendored Signalsmith worklet does at a loop wrap and with a
// negative rate. Findings: (private path).
//
// Usage (from apps/webui/frontend):
//   node scripts/probe-loop-seam-click.mjs --fixture <audio file> --bpm <bpm> --out <dir>
// Renders every variant through the REAL worklet in a headless Chromium
// OfflineAudioContext and writes <variant>.f32 (interleaved stereo float32),
// source.f32 (decoded mono) and manifest.json. Measure with
// `uv run python -m scripts.quality.loop_seam <dir>/manifest.json` from the repo root.
// Never trims by node.latency(): the measurer finds the offset by correlation.
// Exits 1 on any harness failure (missing flag, decode failure, non-determinism).
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';

import { chromium } from '@playwright/test';

const { values } = parseArgs({
	options: { fixture: { type: 'string' }, bpm: { type: 'string' }, out: { type: 'string' } }
});
for (const flag of ['fixture', 'bpm', 'out']) {
	if (values[flag] === undefined) {
		console.error(`[ERROR] missing --${flag}`);
		process.exit(1);
	}
}

const ORIGIN = 'https://loop-seam.invalid';
const SAMPLE_RATE = 44100;
const RENDER_S = 5.0;
const ENGAGE_S = 1.0;
const XFADE_FRAMES = [128, 256, 512];
// Copied from src/lib/rb/stretch-adapter.ts STRETCH_NODE_OPTIONS (a .mjs cannot import the .ts).
const STRETCH_NODE_OPTIONS = {
	numberOfInputs: 1,
	numberOfOutputs: 1,
	outputChannelCount: [2],
	channelCount: 2,
	channelCountMode: 'explicit',
	channelInterpretation: 'speakers'
};

const fixturePath = resolve(values.fixture);
const bpm = Number(values.bpm);
const outDir = resolve(values.out);
const moduleSource = readFileSync(
	'node_modules/signalsmith-stretch/SignalsmithStretch.mjs',
	'utf8'
);
const signalsmithVersion = JSON.parse(
	readFileSync('node_modules/signalsmith-stretch/package.json', 'utf8')
).version;
const fixtureBytes = readFileSync(fixturePath);
const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

const LOOP_BEATS = { '1-32': 1 / 32, '1-8': 1 / 8, '1-4': 1 / 4, 1: 1 };

// Every variant as data; the page builds regions and schedules from it.
function variantPlan() {
	const plan = [];
	for (const [label, beats] of Object.entries(LOOP_BEATS)) {
		plan.push({ id: `today-${label}`, kind: 'today', beats });
	}
	for (const [label, beats] of Object.entries(LOOP_BEATS)) {
		if (beats < 1 / 8) continue;
		for (const n of XFADE_FRAMES)
			plan.push({ id: `baked-xfade${n}-${label}`, kind: 'baked', beats, xfade: n });
	}
	plan.push({ id: 'rate-neg1', kind: 'rate-neg', rate: -1 });
	plan.push({ id: 'reversed-window', kind: 'reversed', rate: 1 });
	plan.push({ id: 'reversed-window-r1.08', kind: 'reversed', rate: 1.08 });
	plan.push({ id: 'reversed-window-split', kind: 'reversed', rate: 1, splitBuffers: true });
	return plan;
}

async function renderInPage(page, plan) {
	return page.evaluate(
		async ({ plan, bpm, sampleRate, renderS, engageS, nodeOptions }) => {
			const create = (await import(`${location.origin}/SignalsmithStretch.mjs`)).default;
			const encoded = await (await fetch(`${location.origin}/fixture`)).arrayBuffer();
			const decoded = await new OfflineAudioContext(2, 1, sampleRate).decodeAudioData(encoded);
			const src = decoded.getChannelData(0).slice();
			const beatS = 60 / bpm;
			// First kick: largest absolute peak in the first 0.5 s.
			let t0Frame = 0;
			for (let i = 0; i < Math.round(0.5 * sampleRate); i += 1) {
				if (Math.abs(src[i]) > Math.abs(src[t0Frame])) t0Frame = i;
			}
			const t0 = t0Frame / sampleRate;
			const S = t0 + 8 * beatS;
			const sFrame = Math.round(S * sampleRate);

			const toBase64 = (bytes) => {
				let binary = '';
				for (let i = 0; i < bytes.length; i += 0x8000) {
					binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
				}
				return btoa(binary);
			};
			const interleave = (channels) => {
				const out = new Float32Array(channels[0].length * 2);
				for (let f = 0; f < channels[0].length; f += 1) {
					out[2 * f] = channels[0][f];
					out[2 * f + 1] = channels[1][f];
				}
				return new Uint8Array(out.buffer);
			};

			// Region appended after the track end for baked and reversed variants, led by
			// PREROLL_S of the audio that would naturally precede it, so the worklet's
			// look-behind window (inputLatency + outputLatency frames) reads real audio.
			const prerollFrames = Math.round(0.5 * sampleRate);
			const regionFor = (v) => {
				if (v.kind === 'baked') {
					const len = Math.round(v.beats * beatS * sampleRate);
					const region = src.slice(sFrame - prerollFrames, sFrame + len);
					const n = Math.min(v.xfade, len);
					// Mixxx 2.5 rule: fade the loop tail into the audio just before loop start.
					for (let i = 0; i < n; i += 1) {
						const g = (i + 1) / n;
						const at = prerollFrames + len - n + i;
						region[at] = region[at] * (1 - g) + src[sFrame - n + i] * g;
					}
					return region;
				}
				if (v.kind === 'reversed') {
					const pivot = sFrame + Math.round(2.0 * sampleRate);
					const region = src.slice(pivot - Math.round(4.0 * sampleRate), pivot + prerollFrames);
					region.reverse();
					return region;
				}
				return null;
			};

			const renderOne = async (v) => {
				const context = new OfflineAudioContext({
					numberOfChannels: 2,
					sampleRate,
					length: renderS * sampleRate
				});
				const node = await create(context, nodeOptions);
				let processorError = null;
				node.onprocessorerror = (event) => {
					processorError = `processorerror: ${event.message ?? 'worklet process() threw'}`;
				};
				const region = regionFor(v);
				let R = null;
				if (region === null) {
					await node.addBuffers([src.slice(), src.slice()]);
				} else if (v.splitBuffers) {
					// Second addBuffers chunk: exercises the library's multi-chunk read path.
					await node.addBuffers([src.slice(), src.slice()]);
					await node.addBuffers([region.slice(), region.slice()]);
					R = (src.length + prerollFrames) / sampleRate;
				} else {
					const joined = new Float32Array(src.length + region.length);
					joined.set(src);
					joined.set(region, src.length);
					await node.addBuffers([joined.slice(), joined.slice()]);
					R = (src.length + prerollFrames) / sampleRate;
				}
				node.connect(context.destination);
				const loopS = v.beats ? v.beats * beatS : 0;
				const schedules = [
					{
						output: 0,
						outputTime: 0,
						active: true,
						input: S - engageS,
						rate: 1,
						semitones: 0,
						loopStart: 0,
						loopEnd: 0
					}
				];
				if (v.kind === 'today') {
					schedules.push({
						output: engageS,
						outputTime: engageS,
						active: true,
						input: S,
						rate: 1,
						semitones: 0,
						loopStart: S,
						loopEnd: S + loopS
					});
				} else if (v.kind === 'baked') {
					schedules.push({
						output: engageS,
						outputTime: engageS,
						active: true,
						input: R,
						rate: 1,
						semitones: 0,
						loopStart: R,
						loopEnd: R + loopS
					});
				} else if (v.kind === 'rate-neg') {
					schedules.push({
						output: engageS,
						outputTime: engageS,
						active: true,
						input: S + 2.0,
						rate: v.rate,
						semitones: 0,
						loopStart: 0,
						loopEnd: 0
					});
				} else if (v.kind === 'reversed') {
					schedules.push({
						output: engageS,
						outputTime: engageS,
						active: true,
						input: R,
						rate: v.rate,
						semitones: 0,
						loopStart: 0,
						loopEnd: 0
					});
				}
				let error = null;
				try {
					for (const s of schedules) await node.schedule(s);
				} catch (e) {
					error = String(e);
				}
				const rendered = await context.startRendering();
				error = error ?? processorError;
				const bytes = interleave([rendered.getChannelData(0), rendered.getChannelData(1)]);
				const digest = await crypto.subtle.digest('SHA-256', bytes.buffer);
				const hex = Array.from(new Uint8Array(digest))
					.map((b) => b.toString(16).padStart(2, '0'))
					.join('');
				return {
					id: v.id,
					kind: v.kind,
					beats: v.beats ?? null,
					loop_s: loopS,
					xfade_frames: v.xfade ?? null,
					rate: v.rate ?? 1,
					region_start_s: R,
					split_buffers: v.splitBuffers ?? false,
					schedules,
					error,
					sha256: hex,
					base64: toBase64(bytes),
					latency_s: await node.latency()
				};
			};

			const variants = [];
			for (const v of plan) variants.push(await renderOne(v));
			const repeat = await renderOne(plan.find((v) => v.id === 'today-1-4'));
			const first = variants.find((v) => v.id === 'today-1-4');
			if (repeat.sha256 !== first.sha256) throw new Error('non-deterministic render of today-1-4');
			return {
				t0_s: t0,
				loop_start_s: S,
				beat_s: beatS,
				source_base64: toBase64(new Uint8Array(src.buffer)),
				variants
			};
		},
		{
			plan,
			bpm,
			sampleRate: SAMPLE_RATE,
			renderS: RENDER_S,
			engageS: ENGAGE_S,
			nodeOptions: STRETCH_NODE_OPTIONS
		}
	);
}

const browser = await chromium.launch();
try {
	const page = await browser.newPage();
	await page.route(`${ORIGIN}/**`, (route) => {
		const path = new URL(route.request().url()).pathname;
		if (path === '/')
			return route.fulfill({
				status: 200,
				contentType: 'text/html',
				body: '<!doctype html><title>loop seam</title>'
			});
		if (path === '/SignalsmithStretch.mjs')
			return route.fulfill({ status: 200, contentType: 'text/javascript', body: moduleSource });
		if (path === '/fixture')
			return route.fulfill({ status: 200, contentType: 'audio/mp4', body: fixtureBytes });
		return route.fulfill({ status: 404, body: 'not served' });
	});
	page.on('console', (message) => console.log(`[page] ${message.text()}`));
	await page.goto(`${ORIGIN}/`);
	const result = await renderInPage(page, variantPlan());
	mkdirSync(outDir, { recursive: true });
	writeFileSync(join(outDir, 'source.f32'), Buffer.from(result.source_base64, 'base64'));
	const variants = result.variants.map(({ base64, latency_s, ...rest }) => {
		const file = `${rest.id}.f32`;
		writeFileSync(join(outDir, file), Buffer.from(base64, 'base64'));
		console.log(`[OK] ${rest.id.padEnd(24)} ${rest.error ?? 'rendered'}`);
		return { ...rest, file };
	});
	const manifest = {
		fixture: basename(fixturePath),
		fixture_sha256: sha256(fixtureBytes),
		sample_rate: SAMPLE_RATE,
		render_s: RENDER_S,
		engage_s: ENGAGE_S,
		bpm,
		beat_s: result.beat_s,
		t0_s: result.t0_s,
		loop_start_s: result.loop_start_s,
		node_latency_s: result.variants[0].latency_s,
		chromium_version: browser.version(),
		signalsmith_version: signalsmithVersion,
		source_file: 'source.f32',
		source_sha256: sha256(readFileSync(join(outDir, 'source.f32'))),
		variants
	};
	writeFileSync(join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2));
	console.log(`[OK] manifest ${join(outDir, 'manifest.json')}`);
} catch (error) {
	console.error(`[ERROR] ${error.message}`);
	process.exitCode = 1;
} finally {
	await browser.close();
}
