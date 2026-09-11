/**
 * Q18 rung 1, on a real browser with a real FLAC file.
 *
 * The unit tests prove the POLICY (sniffing, the three refusals, worker
 * lifecycle) against an injected decoder. They cannot prove the thing most
 * likely to break in practice: that `FLACDecoderWebWorker` actually spawns a
 * worker and compiles its wasm once bundled, and that what comes back is the
 * same audio `decodeAudioData` produces. A rung that silently falls back
 * decodes correctly and buys nothing, and the unit suite would still be green.
 *
 * So this asserts three things a mock cannot:
 *   1. the worker path is TAKEN (refusal === null on every part)
 *   2. the samples are BIT-IDENTICAL to decodeAudioData's - FLAC is lossless
 *      and neither path resamples, so anything less is a decoder bug
 *   3. each width is timed on both lanes and the calibration agrees with the
 *      stopwatch (PERF-STEMDEC-02 adds the 2-part roformer2 arm)
 *
 * Re-runnable: `pnpm test:live:stem-decode-workers`. Prints the measured
 * numbers so a later round can compare against them rather than against a
 * remembered figure. It refuses to run until its audio inputs are pinned by
 * checksum: `Q18_RECORD_FIXTURES=1` records that manifest and decodes nothing.
 *
 * Exit codes, because a run that measured nothing must not read as a pass:
 *   0  every required engine ran and every check passed
 *   1  an engine ran and a check FAILED
 *   2  the real FLAC fixtures are missing (set `Q18_FLAC_DIR`)
 *   3  UNAVAILABLE - an engine could not launch, so this is not evidence
 *   4  the inputs are not the ones the pinned manifest names
 *   5  no pinned manifest, or --record asked to overwrite one that exists
 *   6  --record wrote a manifest; that mode runs no checks and proves nothing
 *
 * 3 is the one worth stating. WebKit is the engine the rung exists for and
 * Chromium is the reason it is a measurement rather than an assumption, so a
 * run that launched neither has a failure count of zero for the same reason a
 * check pointed at nothing does.
 */

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { hostname } from 'node:os';
import { tmpdir } from 'node:os';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium, webkit } from '@playwright/test';

import { buildManifest, manifestGate } from './fixture-manifest.mjs';
import { laneMarginFrom } from './lane-oracle.mjs';
import {
	FOUR_PARTS,
	TWO_PARTS,
	agreeLine,
	aliasTwoPartBundle,
	appendLedger,
	independenceHolds,
	laneTimingsToRows,
	missingFixtureMessage
} from './stem-decode-kpis.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '../..');
const REPO = path.resolve(FRONTEND, '../../..');
/**
 * A REAL 44.1kHz stereo FLAC. Never synthesized: the claim under test is that
 * a wasm decoder agrees with WebKit's on real audio, and a generated tone
 * exercises neither's edge cases.
 *
 * `data/` is gitignored and does not exist in a fresh worktree, so the path is
 * overridable and the failure names the override instead of a bare ENOENT.
 */
const FIXTURE_DIR =
	process.env.Q18_FLAC_DIR ?? path.join(REPO, 'data/datasets/jamendolyrics-vocals');
const PORT = 8719;
/**
 * Where this run records what it decoded, and what it verifies against.
 *
 * Overridable so a reviewer can point a re-run at the manifest quoted in an
 * evidence comment and have the run refuse to proceed on different audio.
 * Default lives in the repo's gitignored scratch dir: the files it pins are
 * per-machine, so a tracked default would fail for everyone but the machine
 * that wrote it.
 */
const MANIFEST_PATH =
	process.env.Q18_FIXTURE_MANIFEST ?? path.join(REPO, '.tmp/q18-stem-decode-fixtures.json');
/** One least-significant bit of a 16-bit sample, in float. */
const LSB_16_BIT = 1 / 32768;

function parseArgs() {
	const args = process.argv.slice(2);
	let ledgerPath = null;
	for (let i = 0; i < args.length; i++) {
		if (args[i] === '--ledger') {
			ledgerPath = args[i + 1] ?? path.join(REPO, 'docs/perf/kpi-ledger.json');
		}
	}
	return {
		recordMode: args.includes('--record') || process.env.Q18_RECORD_FIXTURES === '1',
		appendLedger: ledgerPath !== null || process.env.Q18_APPEND_LEDGER === '1',
		ledgerPath: ledgerPath ?? path.join(REPO, 'docs/perf/kpi-ledger.json')
	};
}

const CLI = parseArgs();
/**
 * Pinning the inputs is a MODE of its own, not a thing an ordinary run does
 * on its way past. A run that may write the contract it is about to check
 * itself against has verified nothing, and on a fresh checkout - where the
 * gitignored default does not exist - that would be every run.
 */
const RECORD_MODE = CLI.recordMode;
/**
 * The margin the implementation actually uses, READ from it.
 *
 * The oracle below compared the stopwatch against 1.0, so a worker win inside
 * the margin - which production deliberately declines, because noise must not
 * flip a session-long choice - was reported as the calibration disagreeing
 * with the stopwatch and failed the run. Read rather than copied: a duplicated
 * constant is a value that rots the first time the real one moves, and the
 * whole point of this check is that the two agree.
 */
const LANE_MARGIN = (() => {
	const margin = laneMarginFrom(
		readFileSync(path.join(FRONTEND, 'src/lib/player/decode/flac-stem-decode.ts'), 'utf8')
	);
	if (margin === null) {
		// Not defaulted: a margin this could not read is a comparison it cannot
		// make, and guessing 1.25 would silently pass against a moved constant.
		console.log('could not read LANE_MARGIN out of flac-stem-decode.ts');
		process.exit(1);
	}
	return margin;
})();

/** Bundle the module under test the way the app bundles it. */
async function bundleModule() {
	const out = path.join(tmpdir(), 'q18-stem-decode-bundle.mjs');
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

const PAGE = `<!doctype html><meta charset="utf-8"><title>q18</title>`;

async function main() {
	// FOUR DISTINCT files, not one file four times. A decoder that caches by
	// content would win the baseline lane on identical inputs and the whole
	// comparison would measure the cache, not the decoder.
	const fixtures = existsSync(FIXTURE_DIR)
		? readdirSync(FIXTURE_DIR)
				.filter((name) => name.endsWith('.flac'))
				.sort()
				.slice(0, FOUR_PARTS.length)
				.map((name) => path.join(FIXTURE_DIR, name))
		: [];
	if (fixtures.length < FOUR_PARTS.length) {
		for (const line of missingFixtureMessage({
			found: fixtures.length,
			required: FOUR_PARTS.length,
			fixtureDir: FIXTURE_DIR
		})) {
			console.log(line);
		}
		process.exit(2);
	}
	// Read and VERIFY the inputs before spending an esbuild on them: a run
	// whose subject is wrong is not worth preparing, and the refusal below
	// should cost seconds rather than a bundle plus two browser launches.
	const flacs = await Promise.all(fixtures.map((file) => readFile(file)));
	const byPart = Object.fromEntries(FOUR_PARTS.map((part, i) => [part, flacs[i]]));
	const twoPartAlias = aliasTwoPartBundle(byPart);
	byPart.instrumental = twoPartAlias.instrumental;

	console.log(
		'2-part arm: vocals plus the drums fixture aliased as instrumental (not a RoFormer mixdown)'
	);

	// PROVENANCE. A basename and a size do not identify audio, and this
	// directory is mutable, so a later run of the same command can pass
	// against different files while being cited as the same evidence. What
	// this run read is therefore printed by content and checked against a
	// manifest pinned EARLIER, by the separate --record mode below. Anything
	// but a clean verification stops the run before a browser opens: a
	// measurement of the wrong subject is not worth taking, and a run that
	// pinned its own subject on the way past has verified nothing.
	const manifest = buildManifest(
		fixtures.map((file, i) => ({ part: FOUR_PARTS[i], file, bytes: flacs[i] }))
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
			// NOT a missing manifest: a file that cannot be read is a pin that
			// cannot be honored, and it must not degrade into "record a new one".
			recorded = `unparseable: ${String(exc).split('\n')[0]}`;
		}
	}
	const gate = manifestGate({ recordMode: RECORD_MODE, manifestExists, recorded, observed: manifest });
	if (gate.action === 'record') {
		mkdirSync(path.dirname(MANIFEST_PATH), { recursive: true });
		writeFileSync(MANIFEST_PATH, `${JSON.stringify(manifest, null, '\t')}\n`);
		console.log(`\nRECORDED ${MANIFEST_PATH}`);
		console.log('These four files are now the pinned contract. Have them reviewed:');
		console.log('nothing has been decoded and no acceptance evidence was produced by');
		console.log('this run. Re-run WITHOUT --record to produce evidence against them.');
		process.exit(6);
	}
	if (gate.action === 'refuse-overwrite') {
		console.log(`\n--record refuses to overwrite ${MANIFEST_PATH}`);
		for (const problem of gate.problems) console.log(`  ${problem}`);
		if (gate.problems.length === 0) console.log('  (it already pins exactly these files)');
		console.log('Re-pinning is deliberate or it is not re-pinning: delete that file first.');
		process.exit(5);
	}
	if (gate.action === 'missing') {
		console.log(`\nNO PINNED MANIFEST at ${MANIFEST_PATH}`);
		console.log('This run will not decide for itself which audio counts as the fixture');
		console.log('contract and then pass against it. Pin the inputs deliberately with');
		console.log('  Q18_FLAC_DIR=/abs/dir Q18_RECORD_FIXTURES=1 pnpm test:live:stem-decode-workers');
		console.log('or point Q18_FIXTURE_MANIFEST at the manifest the evidence you are');
		console.log('reproducing was captured against.');
		process.exit(5);
	}
	if (gate.action === 'mismatch') {
		console.log(`\nPROVENANCE MISMATCH against ${MANIFEST_PATH}`);
		for (const problem of gate.problems) console.log(`  ${problem}`);
		console.log('\nThis run would measure audio the pinned contract never covered. Point');
		console.log('Q18_FLAC_DIR at the pinned inputs, or re-pin deliberately (delete the');
		console.log('file above, re-run with --record) and recapture the evidence - do not');
		console.log('delete it to go green.');
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
			res.writeHead(200, { 'content-type': 'audio/flac' }).end(body);
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
	/** Engines that actually launched AND completed their checks. */
	const ran = [];
	/** Engines that could not launch, with the reason, for the UNAVAILABLE report. */
	const unavailable = [];
	/** Per-engine width timings for optional ledger append. */
	const ledgerEngineResults = [];

	for (const [name, engine] of engines) {
		let browser;
		try {
			browser = await engine.launch();
		} catch (exc) {
			// NOT a skip. A missing browser is a capability this run does not
			// have, and the run says so at the end rather than letting a zero
			// failure count read as evidence.
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
			async ({ four, two }) => {
				const decode = await import('/decode.mjs');
				const ctx = new OfflineAudioContext(2, 1, 44100);

				const fetchParts = async (parts) => {
					const entries = await Promise.all(
						parts.map(async (part) => [part, await (await fetch('/stem/' + part)).arrayBuffer()])
					);
					return Object.fromEntries(entries);
				};

				decode.stemDecodeSession.resetLane();
				decode.stemDecodeSession.resetPool();

				const fourBytes = await fetchParts(four);
				const twoBytes = {
					vocals: fourBytes.vocals,
					instrumental: fourBytes.drums
				};

				const reference4 = Object.fromEntries(
					await Promise.all(
						four.map(async (part) => [part, await ctx.decodeAudioData(fourBytes[part].slice(0))])
					)
				);
				const reference2 = Object.fromEntries(
					await Promise.all(
						two.map(async (part) => [part, await ctx.decodeAudioData(twoBytes[part].slice(0))])
					)
				);

				await decode.decodeStemParts(ctx, fourBytes, four);
				await decode.decodeStemParts(ctx, fourBytes, four);
				const lane4AfterFour = decode.stemDecodeSession.lane(4);
				const lane2AfterFour = decode.stemDecodeSession.lane(2);

				await decode.decodeStemParts(ctx, twoBytes, two);
				await decode.decodeStemParts(ctx, twoBytes, two);
				const lane2AfterTwo = decode.stemDecodeSession.lane(2);
				const lane4AfterTwo = decode.stemDecodeSession.lane(4);

				const widths = {};
				for (const [width, parts, bytes, reference] of [
					[4, four, fourBytes, reference4],
					[2, two, twoBytes, reference2]
				]) {
					decode.stemDecodeSession.forceLane('main-thread');
					const tb = performance.now();
					await decode.decodeStemParts(ctx, bytes, parts);
					const baselineMs = Math.round(performance.now() - tb);

					decode.stemDecodeSession.forceLane('workers');
					const t1 = performance.now();
					const run = await decode.decodeStemParts(ctx, bytes, parts);
					const workerMs = Math.round(performance.now() - t1);

					let maxAbsDiff = 0;
					let comparedSamples = 0;
					for (const part of parts) {
						const a = reference[part];
						const b = run.buffers[part];
						if (a.length !== b.length || a.numberOfChannels !== b.numberOfChannels) {
							return {
								shapeMismatch: { width, part, a: a.length, b: b.length }
							};
						}
						for (let ch = 0; ch < a.numberOfChannels; ch++) {
							const x = a.getChannelData(ch);
							const y = b.getChannelData(ch);
							for (let i = 0; i < x.length; i += 97) {
								const d = Math.abs(x[i] - y[i]);
								if (d > maxAbsDiff) maxAbsDiff = d;
								comparedSamples++;
							}
						}
					}

					widths[width] = {
						chosenLane: width === 4 ? lane4AfterFour : lane2AfterTwo,
						baselineMs,
						workerMs,
						reports: run.reports,
						labels: decode.stemDecodeLabels(run.reports),
						maxAbsDiff,
						comparedSamples,
						frames: reference[parts[0]].length,
						sampleRate: reference[parts[0]].sampleRate,
						pooled: decode.stemDecodeSession.pooled()
					};
				}

				return {
					independence: {
						lane4AfterFour,
						lane2AfterFour,
						lane2AfterTwo,
						lane4AfterTwo
					},
					widths
				};
			},
			{ four: FOUR_PARTS, two: TWO_PARTS }
		);
		await browser.close();

		const engineLedger = { engine: name, widths: {} };

		if (result.shapeMismatch !== undefined) {
			const sm = result.shapeMismatch;
			console.log(
				`\n[${name}] FAIL shape mismatch on ${sm.width}-part ${sm.part}: ${sm.a} vs ${sm.b}`
			);
			failures++;
			ran.push(name);
			continue;
		}

		for (const width of [4, 2]) {
			const w = result.widths[width];
			console.log(`\n[${name}] ${w.frames} frames @ ${w.sampleRate} Hz, ${width} parts`);
			console.log(`  labels:            ${JSON.stringify(w.labels)}`);
			console.log(`  pooled decoders:   ${w.pooled}`);
			console.log(`  lane chosen here:  ${w.chosenLane}`);
			console.log(`  decodeAudioData:   ${w.baselineMs} ms (${width}-way)`);
			console.log(`  wasm in workers:   ${w.workerMs} ms (${width}-way, warm pool)`);
			console.log(`  speed-up:          ${(w.baselineMs / w.workerMs).toFixed(2)}x`);
			console.log(
				`  max |sample diff|: ${w.maxAbsDiff} over ${w.comparedSamples} compared samples`
			);

			const agreement = agreeLine({
				chosenLane: w.chosenLane,
				baselineMs: w.baselineMs,
				workerMs: w.workerMs,
				margin: LANE_MARGIN
			});
			console.log(agreement.text);
			engineLedger.widths[width] = {
				baselineMs: w.baselineMs,
				workerMs: w.workerMs,
				chosenLane: w.chosenLane,
				agrees: agreement.agrees
			};

			if (!agreement.agrees) {
				console.log('  FAIL the runtime calibration picked the slower lane');
				failures++;
			}
			const tookWorkers = w.reports.every((r) => r.viaWorker && r.refusal === null);
			if (!tookWorkers) {
				console.log(`  FAIL the worker path was refused: ${JSON.stringify(w.reports)}`);
				failures++;
			}
			if (w.maxAbsDiff >= LSB_16_BIT) {
				console.log(
					`  FAIL decode disagreed by ${w.maxAbsDiff} >= one 16-bit LSB (${LSB_16_BIT})`
				);
				failures++;
			}
			if (w.comparedSamples === 0) {
				console.log(`  FAIL nothing was compared - the check cannot fail, so it proved nothing`);
				failures++;
			}
			if (tookWorkers && w.maxAbsDiff < LSB_16_BIT && w.comparedSamples > 0 && agreement.agrees) {
				console.log('  PASS worker path taken, output agrees to within one 16-bit LSB');
			}
		}

		const ind = result.independence;
		console.log(`  lane(2) after 4-part calibration: ${ind.lane2AfterFour ?? 'null'}`);
		console.log(`  lane(2) after its own two trials: ${ind.lane2AfterTwo ?? 'null'}`);
		if (!independenceHolds(ind)) {
			console.log(
				`  FAIL per-layout independence: lane(4) after four=${ind.lane4AfterFour}, ` +
					`lane(2) after four=${ind.lane2AfterFour}, lane(2) after two=${ind.lane2AfterTwo}, ` +
					`lane(4) after two=${ind.lane4AfterTwo}`
			);
			failures++;
		} else {
			console.log('  PASS per-layout lane verdicts stayed independent');
		}

		ledgerEngineResults.push(engineLedger);
		ran.push(name);
	}
	server.close();

	// The claim this script exists to support is about WebKit specifically -
	// the rung routes around WebKit's single decode thread - and is only a
	// comparison because Chromium disagrees. So BOTH engines are required, and
	// a run that could not launch one has not produced acceptance evidence for
	// it. Reported as UNAVAILABLE and exited nonzero rather than printing OK:
	// zero failures on zero decodes is the shape of a check that cannot fail.
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
		console.log(
			`\nUNAVAILABLE: ${ran.length} of ${engines.length} required engines ran, so this is not acceptance evidence`
		);
		process.exit(3);
	}

	if (CLI.appendLedger) {
		const rows = laneTimingsToRows(ledgerEngineResults, {
			date: new Date().toISOString().slice(0, 10),
			round: 'issue-2057',
			machine: hostname(),
			source:
				'pnpm test:live:stem-decode-workers PERF-STEMDEC-02, <N> distinct 44.1kHz FLACs, Playwright <engine>, warm pool, decode only',
			captureId: 'issue-2057-stemdec-02',
			fixtureCount: FOUR_PARTS.length
		});
		appendLedger(CLI.ledgerPath, rows);
		console.log(`\nappended ${rows.length} KPI rows to ${CLI.ledgerPath}`);
	}

	console.log('\nOK');
}

await main();
