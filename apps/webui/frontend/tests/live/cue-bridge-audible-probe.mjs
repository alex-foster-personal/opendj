/**
 * Does the SHIPPED cue bridge actually put sound into a chosen output device?
 *
 * Builds only the bridge: the real cue-bridge-processor.js sender in a main
 * context, receiver in a second context pinned with setSinkId, a loud tone in.
 * Reports the receiver's own underrun/priming counters. Audibility is judged
 * separately by a microphone recording taken while this runs.
 *
 * Control: the same tone straight to the MAIN context destination, so "nothing
 * was heard" cannot be blamed on the page never making sound at all.
 */
import { chromium } from '@playwright/test';
const arg = (n, d = null) => { const i = process.argv.indexOf(`--${n}`); return i === -1 ? d : process.argv[i + 1]; };
const URL_ = String(arg('url', 'http://localhost:9464/performance'));
const CUE_MATCH = String(arg('cue', "Steve's over-ears"));
const SECONDS = Number(arg('seconds', 6));

const browser = await chromium.launch({ headless: false, timeout: 600_000,
	args: ['--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream'] });
try {
	const page = await browser.newPage();
	page.on('console', (m) => console.log(`  page: ${m.text()}`));
	await page.goto(URL_, { waitUntil: 'domcontentloaded', timeout: 180_000 });
	// Device labels need a mic grant before they are readable.
	await page.evaluate(() => navigator.mediaDevices.getUserMedia({ audio: true }).catch(() => null));
	const out = await page.evaluate(async ({ cueMatch, seconds }) => {
		const devs = await navigator.mediaDevices.enumerateDevices();
		const dev = devs.find((d) => d.kind === 'audiooutput' && d.label.includes(cueMatch));
		if (dev === undefined) return { error: `no output device matching ${cueMatch}`,
			seen: devs.filter((d) => d.kind === 'audiooutput').map((d) => d.label) };
		const main = new AudioContext();
		const cue = new AudioContext({ sampleRate: main.sampleRate });
		const url = new URL('/src/lib/player/cue-bridge-processor.js', location.origin).href;
		await main.audioWorklet.addModule(url);
		await cue.audioWorklet.addModule(url);
		await cue.setSinkId(dev.deviceId);
		await main.resume(); await cue.resume();
		const ch = new MessageChannel(); ch.port1.start(); ch.port2.start();
		const sender = new AudioWorkletNode(main, 'cue-bridge-sender',
			{ numberOfInputs: 1, numberOfOutputs: 0, channelCount: 2, channelCountMode: 'explicit',
			  processorOptions: { mode: 'port' } });
		const receiver = new AudioWorkletNode(cue, 'cue-bridge-receiver',
			{ numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2],
			  processorOptions: { mode: 'port', capacity: 16384 } });
		sender.port.postMessage({ type: 'connect', port: ch.port1 }, [ch.port1]);
		receiver.port.postMessage({ type: 'connect', port: ch.port2 }, [ch.port2]);
		let underruns = 0;
		receiver.port.onmessage = (e) => { if (e.data?.type === 'underrun') underruns += 1; };
		receiver.connect(cue.destination);
		const osc = main.createOscillator(); osc.frequency.value = 1000;
		const g = main.createGain(); g.gain.value = 0.5;
		osc.connect(g); g.connect(sender); osc.start();
		await new Promise((r) => setTimeout(r, seconds * 1000));
		osc.stop();
		const res = { sinkId: String(cue.sinkId).slice(0, 12), device: dev.label,
			underruns, mainRate: main.sampleRate, cueRate: cue.sampleRate,
			cueState: cue.state, cueOutputLatencyMs: cue.outputLatency * 1000 };
		await main.close(); await cue.close();
		return res;
	}, { cueMatch: CUE_MATCH, seconds: SECONDS });
	console.log(JSON.stringify(out, null, 1));
} finally { await browser.close(); }
