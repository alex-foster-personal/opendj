/**
 * I18 remote speaker, end to end on one machine: two engines, two Chromium
 * pages, real WebRTC, real audio (REMOTE-SPK-8119e, REMOTE-SPK-8119f).
 *
 * What is real: both engines run the shipped routers (state, commands,
 * remote speaker) under uvicorn; the receiver's LAN listener is the shipped
 * one, bound 0.0.0.0; the sender's engine relays over real HTTP; each page
 * runs the shipped `remote-speaker-orders.ts` + `remote-speaker.ts` from its
 * own engine's command queue. What is a harness: the order loop (the live
 * app's is agent-orders.ts, which needs the whole performance page) and the
 * sender's MAIN (an OscillatorNode into a GainNode, standing in for the
 * master bus).
 *
 * Every step is driven over HTTP, as an agent would.
 *
 * Run (from apps/webui/frontend; the repo .venv must exist):
 *
 *   node tests/live/remote-speaker-loopback.mjs
 *
 * Regression lines:
 *  - if the offer/answer never crosses the relay then POST /link is not 200 linked
 *  - if the receiver plays nothing then its RMS stays under RMS_FLOOR
 *  - if the RMS is not the sender's audio then muting the sender does not drop it
 *  - if the speaker reads linked without a sender then the pre-link control fails
 *
 * Latency is LOOPBACK ONLY (one machine, no Wi-Fi): from the sender's gain
 * step to the first receiver analyser block over ONSET_FLOOR, wall clock via
 * performance.timeOrigin + now() in each page. It includes Opus encode/decode
 * and the jitter buffer, and excludes both machines' output device latency.
 */
import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';
import { chromium } from '@playwright/test';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const REPO_ROOT = fileURLToPath(new URL('../../../../..', import.meta.url));
const LIB_ROOT = join(FRONTEND_ROOT, 'src/lib');
const CHROMIUM = process.env.REMOTE_SPEAKER_CHROMIUM ?? '/opt/pw-browsers/chromium';
const PYTHON = join(REPO_ROOT, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const ROUTES = '/api/v1/performance/remote-speaker';
/** A 0.5-amplitude sine is RMS 0.354; Opus at 256 kbit/s keeps it near that. */
const RMS_FLOOR = 0.1;
/** After the sender mutes, the receiver must fall under this. */
const SILENCE_CEILING = 0.01;
const ONSET_FLOOR = 0.05;
const LATENCY_TRIALS = 7;

const ENGINES_PY = String.raw`
import asyncio, json, sys
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from apps.webui.server.routes.commands import router as commands_router
from apps.webui.server.routes.state import router as state_router
from apps.webui.server.routes.performance_remote_speaker import router as rs_router, close_remote_speaker

static = Path(sys.argv[1])

def engine() -> FastAPI:
    app = FastAPI()
    for router in (state_router, commands_router, rs_router):
        app.include_router(router, prefix="/api/v1")
    app.get("/harness.html")(lambda: FileResponse(static / "harness.html"))
    app.get("/harness.js")(lambda: FileResponse(static / "harness.js", media_type="text/javascript"))
    return app

async def main() -> None:
    apps = [engine(), engine()]
    servers = [uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=0, log_level="warning", ws="none")) for a in apps]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        await asyncio.sleep(0.05)
    ports = [s.servers[0].sockets[0].getsockname()[1] for s in servers]
    print(json.dumps({"receiver": ports[0], "sender": ports[1]}), flush=True)
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
    for a in apps:
        await close_remote_speaker(a)
    for s in servers:
        s.should_exit = True
    await asyncio.gather(*tasks)

asyncio.run(main())
`;

const HARNESS_ENTRY = fileURLToPath(new URL('./remote-speaker-harness-entry.ts', import.meta.url));

async function bundleHarness() {
	const dir = mkdtempSync(join(tmpdir(), 'remote-speaker-loopback-'));
	await build({
		entryPoints: [HARNESS_ENTRY],
		bundle: true,
		format: 'esm',
		platform: 'browser',
		alias: { $lib: LIB_ROOT },
		outfile: join(dir, 'harness.js'),
		logLevel: 'silent'
	});
	writeFileSync(join(dir, 'harness.html'), '<!doctype html><meta charset="utf-8"><title>remote speaker</title><script type="module" src="/harness.js"></script>');
	return dir;
}

function startEngines(staticDir) {
	const child = spawn(PYTHON, ['-c', ENGINES_PY, staticDir], { cwd: REPO_ROOT, stdio: ['pipe', 'pipe', 'inherit'] });
	return new Promise((resolve, reject) => {
		let buffered = '';
		child.on('exit', (code) => reject(new Error(`engines exited ${code}`)));
		child.stdout.on('data', (chunk) => {
			buffered += chunk;
			const line = buffered.split('\n')[0];
			if (buffered.includes('\n')) resolve({ child, ports: JSON.parse(line) });
		});
	});
}

async function http(port, method, path, body) {
	const response = await fetch(`http://127.0.0.1:${port}${ROUTES}${path}`, {
		method,
		headers: body === undefined ? undefined : { 'content-type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	return { status: response.status, body: await response.json() };
}

function check(ok, message) {
	if (!ok) throw new Error(`FAIL: ${message}`);
	console.log(`ok  ${message}`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function sampleRms(page, n = 10) {
	const values = [];
	for (let i = 0; i < n; i++) {
		const level = await page.evaluate(() => globalThis.__level());
		if (level.rms === null) throw new Error(`receiver has no level: ${level.reason}`);
		values.push(level.rms);
		await sleep(50);
	}
	return values;
}

const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];

async function main() {
	const staticDir = await bundleHarness();
	const { child, ports } = await startEngines(staticDir);
	const browser = await chromium.launch({
		executablePath: CHROMIUM,
		args: ['--autoplay-policy=no-user-gesture-required', '--disable-features=WebRtcHideLocalIpsWithMdns']
	});
	try {
		const rxPage = await (await browser.newContext()).newPage();
		const txPage = await (await browser.newContext()).newPage();
		for (const page of [rxPage, txPage]) page.on('console', (m) => m.type() === 'error' && console.log(`[page] ${m.text()}`));
		await rxPage.goto(`http://127.0.0.1:${ports.receiver}/harness.html`);
		await txPage.goto(`http://127.0.0.1:${ports.sender}/harness.html`);
		for (const page of [rxPage, txPage]) await page.waitForFunction(() => globalThis.__ready === true || globalThis.__loopError);

		const before = await http(ports.receiver, 'GET', '/speaker');
		check(before.body.status === 'off', `control: speaker reads off before start (${before.body.status})`);
		const started = await http(ports.receiver, 'POST', '/speaker/start', { port: 0 });
		check(started.status === 200 && started.body.status === 'listening', `speaker listening on port ${started.body.port}, code ${started.body.code}`);
		const address = `127.0.0.1:${started.body.port}`;

		const wrong = String((Number(started.body.code) + 1) % 1_000_000).padStart(6, '0');
		const refused = await http(ports.sender, 'POST', '/link', { address, code: wrong });
		check(refused.status === 400 && /refused the code/.test(refused.body.detail), `wrong code refused (${refused.status}: ${refused.body.detail})`);

		await txPage.evaluate(() => globalThis.__step(true));
		const t0 = Date.now();
		const linked = await http(ports.sender, 'POST', '/link', { address, code: started.body.code });
		const linkMs = Date.now() - t0;
		check(linked.status === 200 && linked.body.status === 'linked', `POST /link -> ${linked.body.status} in ${linkMs} ms`);
		check((await http(ports.sender, 'GET', '/link')).body.status === 'linked', 'GET /link reads linked');
		check((await http(ports.receiver, 'GET', '/speaker')).body.status === 'linked', 'GET /speaker reads linked');

		await sleep(1500);
		const loud = await sampleRms(rxPage);
		check(Math.min(...loud) > RMS_FLOOR, `receiver RMS with tone: median ${median(loud).toFixed(4)}, min ${Math.min(...loud).toFixed(4)} (floor ${RMS_FLOOR})`);

		const sentA = await txPage.evaluate(() => globalThis.__sent());
		await sleep(2000);
		const sentB = await txPage.evaluate(() => globalThis.__sent());
		const kbps = ((sentB.bytes - sentA.bytes) * 8) / (sentB.at - sentA.at);
		check(
			sentB.mime === 'audio/opus' && sentB.clock === 48000 && sentB.channels === 2 && /stereo=1/.test(sentB.fmtp) && /maxaveragebitrate=256000/.test(sentB.fmtp),
			`negotiated ${sentB.mime} ${sentB.clock} Hz ${sentB.channels} ch, fmtp ${sentB.fmtp}`
		);
		// A voice-call default sends about 32-40 kbit/s; the music fmtp must lift it well clear.
		check(kbps > 128 && kbps < 300, `sent rate over 2 s: ${kbps.toFixed(1)} kbit/s (ceiling 256, voice default about 32-40)`);

		await txPage.evaluate(() => globalThis.__step(false));
		await sleep(1500);
		const quiet = await sampleRms(rxPage);
		check(Math.max(...quiet) < SILENCE_CEILING, `control: sender muted, receiver RMS max ${Math.max(...quiet).toFixed(5)} (ceiling ${SILENCE_CEILING})`);

		const delays = [];
		for (let i = 0; i < LATENCY_TRIALS; i++) {
			await txPage.evaluate(() => globalThis.__step(false));
			await sleep(800);
			const onset = rxPage.evaluate((floor) => globalThis.__watchOnset(floor, 5000), ONSET_FLOOR);
			await sleep(100);
			const stepAt = await txPage.evaluate(() => globalThis.__step(true));
			delays.push((await onset) - stepAt);
		}
		console.log(`latency (loopback only, gain step -> receiver onset), ${LATENCY_TRIALS} trials: median ${median(delays).toFixed(1)} ms, min ${Math.min(...delays).toFixed(1)} ms, max ${Math.max(...delays).toFixed(1)} ms`);

		const unlinked = await http(ports.sender, 'POST', '/unlink', {});
		check(unlinked.body.status === 'idle', 'POST /unlink -> idle');
		const stopped = await http(ports.receiver, 'POST', '/speaker/stop', {});
		check(stopped.body.status === 'off', 'POST /speaker/stop -> off');
		console.log(`sender orders: ${(await txPage.evaluate(() => globalThis.__orders)).join(', ')}`);
		console.log(`receiver orders: ${(await rxPage.evaluate(() => globalThis.__orders)).join(', ')}`);
		console.log('PASS remote speaker loopback');
	} finally {
		await browser.close();
		child.stdin.end();
		await new Promise((resolve) => child.once('exit', resolve));
	}
}

main().catch((error) => {
	console.error(error);
	process.exit(1);
});
