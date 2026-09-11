/**
 * Q18 rung 3 (PERF-STEMDEC-03): mp3-source stems in WASM workers vs decodeAudioData.
 *
 * Sibling of stem-decode-workers.mjs. Same exit codes. Uses four DISTINCT real
 * mp3 files (not the 49-73 KB phase7-dedup fixtures). Compares worker output
 * to decodeAudioData within one 16-bit LSB after gapless trim, with alignment
 * search inside one MPEG frame when lengths differ.
 */

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium, webkit } from '@playwright/test';

import { buildManifest, manifestGate } from './fixture-manifest.mjs';
import { laneMarginFrom, stopwatchLane } from './lane-oracle.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '../..');
const REPO = path.resolve(FRONTEND, '../../..');
const FIXTURE_DIR = process.env.Q18_MP3_DIR ?? path.join(REPO, 'data/datasets/mp3-stems');
const PARTS = ['vocals', 'drums', 'bass', 'other'];
const PORT = 8720;
const MANIFEST_PATH =
	process.env.Q18_MP3_FIXTURE_MANIFEST ?? path.join(REPO, '.tmp/q18-mp3-stem-decode-fixtures.json');
const RECORD_MODE = process.argv.includes('--record') || process.env.Q18_RECORD_FIXTURES === '1';
const LSB_16_BIT = 1 / 32768;
const LANE_MARGIN = (() => {
	const margin = laneMarginFrom(
		readFileSync(path.join(FRONTEND, 'src/lib/player/decode/stem-decode-lane.ts'), 'utf8')
	);
	if (margin === null) {
		console.log('could not read LANE_MARGIN out of stem-decode-lane.ts');
		process.exit(1);
	}
	return margin;
})();

async function bundleModule() {
	const out = path.join(tmpdir(), 'q18-stem-decode-mpeg-bundle.mjs');
	await new Promise((resolve, reject) => {
		const proc = spawn(
			path.join(FRONTEND, 'node_modules/.bin/esbuild'),
			[
				path.join(FRONTEND, 'src/lib/player/decode/flac-stem-decode.ts'),
				'--bundle',
				'--format=esm',
				'--target=safari16',
				`--outfile=${out}`
			],
			{ cwd: FRONTEND, stdio: ['ignore', 'inherit', 'inherit'] }
		);
		proc.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`esbuild ${code}`))));
	});
	return out;
}

const PAGE = `<!doctype html><meta charset="utf-8"><title>q18-mp3</title>`;

async function main() {
	const fixtures = existsSync(FIXTURE_DIR)
		? readdirSync(FIXTURE_DIR)
				.filter((name) => name.endsWith('.mp3'))
				.sort()
				.slice(0, PARTS.length)
				.map((name) => path.join(FIXTURE_DIR, name))
		: [];
	if (fixtures.length < PARTS.length) {
		console.log(`need ${PARTS.length} distinct .mp3 files, found ${fixtures.length} in ${FIXTURE_DIR}`);
		console.log('point this run at a directory: Q18_MP3_DIR=/abs/dir pnpm test:live:stem-decode-workers-mpeg');
		process.exit(2);
	}
	const mp3s = await Promise.all(fixtures.map((file) => readFile(file)));
	const byPart = Object.fromEntries(PARTS.map((part, i) => [part, mp3s[i]]));

	const manifest = buildManifest(
		fixtures.map((file, i) => ({ part: PARTS[i], file, bytes: mp3s[i] }))
	);
	console.log(`fixture manifest v${manifest.version} from ${FIXTURE_DIR}`);
	for (const entry of manifest.parts) {
		console.log(
			`  part ${entry.part}: ${entry.name} (${(entry.bytes / 1e6).toFixed(1)} MB) sha256 ${entry.sha256}`
		);
	}
	let recorded = null;
	const manifestExists = existsSync(MANIFEST_PATH);
	if (manifestExists) {
		try {
			recorded = JSON.parse(readFileSync(MANIFEST_PATH, 'utf8'));
		} catch (exc) {
			recorded = `unparseable: ${String(exc).split('\n')[0]}`;
		}
	}
	const gate = manifestGate({ recordMode: RECORD_MODE, manifestExists, recorded, observed: manifest });
	if (gate.action === 'record') {
		mkdirSync(path.dirname(MANIFEST_PATH), { recursive: true });
		writeFileSync(MANIFEST_PATH, `${JSON.stringify(manifest, null, '\t')}\n`);
		console.log(`\nRECORDED ${MANIFEST_PATH}`);
		process.exit(6);
	}
	if (gate.action === 'refuse-overwrite') {
		console.log(`\n--record refuses to overwrite ${MANIFEST_PATH}`);
		for (const problem of gate.problems) console.log(`  ${problem}`);
		process.exit(5);
	}
	if (gate.action === 'missing') {
		console.log(`\nNO PINNED MANIFEST at ${MANIFEST_PATH}`);
		console.log('  Q18_MP3_DIR=/abs/dir Q18_RECORD_FIXTURES=1 pnpm test:live:stem-decode-workers-mpeg');
		process.exit(5);
	}
	if (gate.action === 'mismatch') {
		console.log(`\nPROVENANCE MISMATCH against ${MANIFEST_PATH}`);
		for (const problem of gate.problems) console.log(`  ${problem}`);
		process.exit(4);
	}
	console.log(`  verified against ${MANIFEST_PATH}`);

	const moduleSource = await readFile(await bundleModule(), 'utf8');
	const server = createServer((req, res) => {
		if (req.url === '/') {
			res.writeHead(200, { 'content-type': 'text/html' }).end(PAGE);
		} else if (req.url === '/decode.mjs') {
			res.writeHead(200, { 'content-type': 'text/javascript' }).end(moduleSource);
		} else if (req.url?.startsWith('/stem/')) {
			const part = req.url.slice('/stem/'.length);
			const body = byPart[part];
			if (body === undefined) {
				res.writeHead(404).end();
				return;
			}
			res.writeHead(200, { 'content-type': 'audio/mpeg' }).end(body);
		} else {
			res.writeHead(404).end();
		}
	});
	await new Promise((resolve) => server.listen(PORT, '127.0.0.1', resolve));

	const engines = [
		['webkit', webkit],
		['chromium', chromium]
	];
	let failures = 0;
	const ran = [];
	const unavailable = [];
	for (const [name, engine] of engines) {
		let browser;
		try {
			browser = await engine.launch();
		} catch (exc) {
			unavailable.push(`${name}: ${String(exc).split('\n')[0]}`);
			console.log(`[unavailable] ${name} did not launch: ${String(exc).split('\n')[0]}`);
			continue;
		}
		const page = await browser.newPage();
		page.on('console', (msg) => {
			if (msg.type() === 'error') console.log(`  [${name} console.error] ${msg.text()}`);
		});
		await page.goto(`http://127.0.0.1:${PORT}/`);
		const result = await page.evaluate(
			async ({ parts, lsb, margin }) => {
				const decode = await import('/decode.mjs');
				decode.setMpegRungShipped(true);
				const ctx = new OfflineAudioContext(2, 1, 44100);
				const fresh = async () => {
					const entries = await Promise.all(
						parts.map(async (part) => [part, await (await fetch('/stem/' + part)).arrayBuffer()])
					);
					return Object.fromEntries(entries);
				};

				const referenceBytes = await fresh();
				const baseline = Object.fromEntries(
					await Promise.all(
						parts.map(async (p) => [p, await ctx.decodeAudioData(referenceBytes[p].slice(0))])
					)
				);

				const warmT0 = performance.now();
				await decode.decodeStemParts(ctx, await fresh(), parts);
				await decode.decodeStemParts(ctx, await fresh(), parts);
				const warmMs = Math.round(performance.now() - warmT0);
				const pooled = decode.stemDecodeSession.pooled('mpeg');
				const chosenLane = decode.stemDecodeSession.lane(parts.length, 'mpeg');

				decode.stemDecodeSession.forceLane('main-thread');
				const baselineBytes2 = await fresh();
				const tb = performance.now();
				await decode.decodeStemParts(ctx, baselineBytes2, parts);
				const baselineMs = Math.round(performance.now() - tb);

				decode.stemDecodeSession.forceLane('workers');
				const workerBytes = await fresh();
				const t1 = performance.now();
				const run = await decode.decodeStemParts(ctx, workerBytes, parts);
				const workerMs = Math.round(performance.now() - t1);

				function compareBuffers(a, b) {
					if (a.numberOfChannels !== b.numberOfChannels || a.sampleRate !== b.sampleRate) {
						return { shapeMismatch: true };
					}
					const lengthDelta = a.length - b.length;
					let bestOffset = 0;
					let bestDiff = Infinity;
					for (let offset = -1152; offset <= 1152; offset++) {
						let maxD = 0;
						let count = 0;
						for (let ch = 0; ch < a.numberOfChannels; ch++) {
							const x = a.getChannelData(ch);
							const y = b.getChannelData(ch);
							const startX = offset >= 0 ? 0 : -offset;
							const startY = offset >= 0 ? offset : 0;
							const len = Math.min(x.length - startX, y.length - startY);
							for (let i = 0; i < len; i += 97) {
								const d = Math.abs(x[startX + i] - y[startY + i]);
								if (d > maxD) maxD = d;
								count++;
							}
						}
						if (maxD < bestDiff) {
							bestDiff = maxD;
							bestOffset = offset;
						}
					}
					return {
						maxAbsDiff: bestDiff,
						lengthDelta,
						alignOffset: bestOffset,
						comparedSamples: a.numberOfChannels * Math.ceil(a.length / 97)
					};
				}

				let maxAbsDiff = 0;
				let comparedSamples = 0;
				let lengthDelta = 0;
				let alignOffset = 0;
				for (const part of parts) {
					const cmp = compareBuffers(baseline[part], run.buffers[part]);
					if (cmp.shapeMismatch) return { shapeMismatch: part };
					maxAbsDiff = Math.max(maxAbsDiff, cmp.maxAbsDiff);
					comparedSamples += cmp.comparedSamples;
					if (Math.abs(cmp.lengthDelta) > Math.abs(lengthDelta)) {
						lengthDelta = cmp.lengthDelta;
						alignOffset = cmp.alignOffset;
					}
				}

				const faster = baselineMs / workerMs;
				const stopwatchSays = faster > margin ? 'workers' : 'main-thread';
				return {
					reports: run.reports,
					labels: decode.stemDecodeLabels(run.reports),
					baselineMs,
					workerMs,
					warmMs,
					pooled,
					chosenLane,
					maxAbsDiff,
					comparedSamples,
					lengthDelta,
					alignOffset,
					frames: baseline[parts[0]].length,
					sampleRate: baseline[parts[0]].sampleRate,
					stopwatchSays,
					faster
				};
			},
			{ parts: PARTS, lsb: LSB_16_BIT, margin: LANE_MARGIN }
		);
		await browser.close();

		console.log(`\n[${name}] ${result.frames} frames @ ${result.sampleRate} Hz, ${PARTS.length} parts`);
		if (result.shapeMismatch !== undefined) {
			console.log(`  FAIL shape mismatch on ${result.shapeMismatch}`);
			failures++;
			ran.push(name);
			continue;
		}
		console.log(`  labels:            ${JSON.stringify(result.labels)}`);
		console.log(`  pooled decoders:   ${result.pooled}`);
		console.log(`  lane chosen here:  ${result.chosenLane}`);
		console.log(`  calibration cost:  ${result.warmMs} ms`);
		console.log(`  decodeAudioData:   ${result.baselineMs} ms (4-way)`);
		console.log(`  wasm in workers:   ${result.workerMs} ms (4-way, warm pool)`);
		console.log(`  speed-up:          ${result.faster.toFixed(2)}x`);
		console.log(
			`  max |sample diff|: ${result.maxAbsDiff} lengthDelta=${result.lengthDelta} alignOffset=${result.alignOffset}`
		);
		const agrees = result.chosenLane === result.stopwatchSays;
		console.log(
			`  calibration chose ${result.chosenLane}; stopwatch says ${result.stopwatchSays}` +
				` (workers ${result.faster.toFixed(2)}x vs ${LANE_MARGIN}x margin) -> ${agrees ? 'AGREE' : 'DISAGREE'}`
		);
		if (!agrees) failures++;
		const tookWorkers = result.reports.every((r) => r.viaWorker && r.refusal === null);
		if (!tookWorkers) {
			console.log(`  FAIL worker path refused: ${JSON.stringify(result.reports)}`);
			failures++;
		}
		if (Math.abs(result.lengthDelta) > 1152) {
			console.log(`  FAIL lengthDelta ${result.lengthDelta} > 1152 after gapless trim`);
			failures++;
		}
		if (result.maxAbsDiff >= LSB_16_BIT) {
			console.log(`  FAIL maxAbsDiff ${result.maxAbsDiff} >= LSB ${LSB_16_BIT}`);
			failures++;
		}
		if (result.comparedSamples === 0) failures++;
		if (tookWorkers && result.maxAbsDiff < LSB_16_BIT && result.comparedSamples > 0) {
			console.log('  PASS worker path taken, output agrees to within one 16-bit LSB');
		}
		ran.push(name);
	}
	server.close();

	console.log(`\nengines that ran: ${ran.length === 0 ? '(none)' : ran.join(', ')}`);
	if (unavailable.length > 0) {
		console.log(`UNAVAILABLE: ${unavailable.join('; ')}`);
		console.log('install them with: pnpm exec playwright install webkit chromium');
	}
	if (failures > 0) {
		console.log(`\nFAILED: ${failures} check(s) across ${ran.length} engine(s)`);
		process.exit(1);
	}
	if (ran.length < engines.length) {
		console.log(`\nUNAVAILABLE: ${ran.length} of ${engines.length} required engines ran`);
		process.exit(3);
	}
	console.log('\nOK');
}

await main();
